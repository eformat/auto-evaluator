import io
import json
import time
import random
import pypdf
import logging
import itertools
import pandas as pd
from typing import Dict, List
from llama_index import Document
from langchain.llms import Anthropic
from langchain.vectorstores import FAISS
from langchain.chains import RetrievalQA
from langchain.chat_models import ChatOpenAI
from langchain.chains import QAGenerationChain
from langchain.retrievers import SVMRetriever
from langchain.evaluation.qa import QAEvalChain
from langchain.retrievers import TFIDFRetriever
from langchain.evaluation.qa import QAEvalChain
from sse_starlette.sse import EventSourceResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, File, UploadFile, Form
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.embeddings.openai import OpenAIEmbeddings
from gpt_index import GPTSimpleVectorIndex, LLMPredictor, ServiceContext
from langchain.text_splitter import RecursiveCharacterTextSplitter, CharacterTextSplitter
from text_utils import GRADE_DOCS_PROMPT, GRADE_ANSWER_PROMPT, GRADE_DOCS_PROMPT_FAST, GRADE_ANSWER_PROMPT_FAST

def generate_eval(text, N, chunk, logger):

    # Generate N questions from context of chunk chars
    # IN: text, N questions, chunk size to draw question from in the doc
    # OUT: list of JSON

    logger.info("`Generating eval QA pair ...`")
    n = len(text)
    starting_indices = [random.randint(0, n-chunk) for _ in range(N)]
    sub_sequences = [text[i:i+chunk] for i in starting_indices]
    chain = QAGenerationChain.from_llm(ChatOpenAI(temperature=0))
    eval_set = []
    
    for i, b in enumerate(sub_sequences):
        try:
            qa = chain.run(b)
            eval_set.append(qa)
        except:
            logger.error("Error on question %s"%i)
    eval_pair = list(itertools.chain.from_iterable(eval_set))
    return eval_pair


def split_texts(text, chunk_size, overlap, split_method, logger):

    # Split text
    # IN: text, chunk size, overlap
    # OUT: list of str splits

    logger.info("`Splitting doc ...`")
    if split_method == "RecursiveTextSplitter":
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size,
                                                       chunk_overlap=overlap)
    elif split_method == "CharacterTextSplitter":
        text_splitter = CharacterTextSplitter(separator=" ",
                                              chunk_size=chunk_size,
                                              chunk_overlap=overlap)
    splits = text_splitter.split_text(text)
    return splits

def make_llm(model_version):

    # Make LLM
    # IN: version
    # OUT: llm

    if (model_version == "gpt-3.5-turbo") or (model_version == "gpt-4"):
        llm = ChatOpenAI(model_name=model_version, temperature=0)
    elif model_version == "anthropic":
        llm = Anthropic(temperature=0)
    return llm

def make_retriever(splits, retriever_type, embeddings, num_neighbors, llm, logger):

    # Make document retriever
    # IN: list of str splits, retriever type, embedding type, number of neighbors for retrieval, llm
    # OUT: retriever

    logger.info("`Making retriever ...`")
    # Set embeddings
    if embeddings == "OpenAI":
        embd = OpenAIEmbeddings()
    elif embeddings == "HuggingFace":
        embd = HuggingFaceEmbeddings()

    # Select retriever
    if retriever_type == "similarity-search":
        try:
            vectorstore = FAISS.from_texts(splits, embd)
        except ValueError:
            print("`Error using OpenAI embeddings (disallowed TikToken token in the text). Using HuggingFace.`", icon="⚠️")
            vectorstore = FAISS.from_texts(splits, HuggingFaceEmbeddings())
        retriever = vectorstore.as_retriever(k=num_neighbors)
    elif retriever_type == "SVM":
        retriever = SVMRetriever.from_texts(splits,embd)
    elif retriever_type == "TF-IDF":
        retriever = TFIDFRetriever.from_texts(splits)
    elif retriever_type == "Llama-Index":
        documents = [Document(t) for t in splits]
        llm_predictor = LLMPredictor(llm)
        context = ServiceContext.from_defaults(chunk_size_limit=512,llm_predictor=llm_predictor)
        retriever = GPTSimpleVectorIndex.from_documents(documents, service_context=context)
    return retriever


def make_chain(llm, retriever, retriever_type):

    # Make chain
    # IN: model version, retriever
    # OUT: chain

    if retriever_type != "Llama-Index":
        qa = RetrievalQA.from_chain_type(llm,
                                        chain_type="stuff",
                                        retriever=retriever,
                                        input_key="question")
    elif retriever_type == "Llama-Index":
        qa = retriever
    
    return qa

def grade_model_answer(predicted_dataset, predictions, grade_answer_prompt, logger):

    # Grade the distilled answer
    # IN: ground truth, model predictions
    # OUT: list of scores

    logger.info("`Grading model answer ...`")
    if grade_answer_prompt == "Fast":
        prompt = GRADE_ANSWER_PROMPT_FAST
    else:
        prompt = GRADE_ANSWER_PROMPT

    eval_chain = QAEvalChain.from_llm(llm=ChatOpenAI(model_name="gpt-3.5-turbo", temperature=0), 
                                      prompt=prompt)
    graded_outputs = eval_chain.evaluate(predicted_dataset,
                                         predictions,
                                         question_key="question",
                                         prediction_key="result")
    return graded_outputs

def grade_model_retrieval(gt_dataset, predictions, grade_docs_prompt, logger):
    
    # Grade the docs retrieval
    # IN: ground truth, model predictions
    # OUT: list of scores

    logger.info("`Grading relevance of retrieved docs ...`")
    if grade_docs_prompt == "Fast":
        prompt = GRADE_DOCS_PROMPT_FAST
    else:
        prompt = GRADE_DOCS_PROMPT

    eval_chain = QAEvalChain.from_llm(llm=ChatOpenAI(model_name="gpt-3.5-turbo", temperature=0), 
                                      prompt=prompt)
    graded_outputs = eval_chain.evaluate(gt_dataset,
                                         predictions,
                                         question_key="question",
                                         prediction_key="result")
    return graded_outputs

def run_eval(chain, retriever, eval_qa_pair, grade_prompt, retriever_type, num_neighbors, logger):

    # Compute eval
    # IN: chain, retriever, eval question, flag for docs retrieval prompt
    # OUT: list of scores for answers and retrieval, latency, predictions

    logger.info("`Running eval ...`")
    predictions = []
    retrieved_docs = []
    gt_dataset = []
    latency = []
    
    # Get answer and log latency
    start_time = time.time()
    if retriever_type != "Llama-Index":
        predictions.append(chain(eval_qa_pair))
    elif retriever_type == "Llama-Index":
        answer = chain.query(eval_qa_pair["question"],similarity_top_k=num_neighbors)
        predictions.append({"question": eval_qa_pair["question"], "answer": eval_qa_pair["answer"],"result": answer.response})
    gt_dataset.append(eval_qa_pair)
    end_time = time.time()
    elapsed_time = end_time - start_time
    latency.append(elapsed_time)

    # Extract text from retrieved docs
    retrieved_doc_text = ""
    if retriever_type != "Llama-Index":
        docs=retriever.get_relevant_documents(eval_qa_pair["question"])
        for i,doc in enumerate(docs):
            retrieved_doc_text += "Doc %s: "%str(i+1) + doc.page_content + " "
    elif retriever_type == "Llama-Index":
        for i, doc in enumerate(answer.source_nodes):
            retrieved_doc_text += "Doc %s: "%str(i+1) + doc.node.text + " "
    
    # Log 
    retrieved = {"question": eval_qa_pair["question"], "answer": eval_qa_pair["answer"], "result": retrieved_doc_text}
    retrieved_docs.append(retrieved)
        
    # Grade
    graded_answers = grade_model_answer(gt_dataset, predictions, grade_prompt, logger)
    graded_retrieval = grade_model_retrieval(gt_dataset, retrieved_docs, grade_prompt, logger)
    return graded_answers, graded_retrieval, latency, predictions


app = FastAPI()

origins = [
    "http://localhost:3000",
    "localhost:3000",
    "https://evaluator-ui.vercel.app/"
    "https://evaluator-ui.vercel.app"
    "evaluator-ui.vercel.app/"
    "evaluator-ui.vercel.app"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"message": "Welcome to the QA Evaluator!"}


def run_evaluator(
    files,
    num_eval_questions,
    chunk_chars,
    overlap,
    split_method,
    retriever_type,
    embeddings,
    model_version,
    grade_prompt,
    num_neighbors,
    test_dataset
):

    # Set up logging
    logging.config.fileConfig('logging.conf', disable_existing_loggers=False)
    logger = logging.getLogger(__name__)

    # Read content of files
    texts = []
    fnames = []
    for file in files:
        logger.info("Reading file: {}".format(file.filename))
        contents = file.file.read()
        # PDF file
        if file.content_type == 'application/pdf':
            logger.info("File {} is a PDF".format(file.filename))
            pdf_reader = pypdf.PdfReader(io.BytesIO(contents))
            text = ""
            for page in pdf_reader.pages:
                text += page.extract_text()
            texts.append(text)
            fnames.append(file.filename)
        # Text file
        elif file.content_type == 'text/plain':
            logger.info("File {} is a TXT".format(file.filename))
            texts.append(contents.decode())
            fnames.append(file.filename)
        else:
            logger.warning(
                "Unsupported file type for file: {}".format(file.filename))
    text =  " ".join(texts)
    
    logger.info("Splitting texts")
    splits = split_texts(text, chunk_chars, overlap, split_method, logger)

    logger.info("Make LLM")
    llm = make_llm(model_version)
    
    logger.info("Make retriever")
    retriever = make_retriever(splits, retriever_type, embeddings, num_neighbors, llm, logger)
    
    logger.info("Make chain")
    qa_chain = make_chain(llm,retriever,retriever_type)
    
    for i in range(num_eval_questions):

        # Generate one question
        if i < len(test_dataset):
            eval_pair = test_dataset[i]
        else:
            eval_pair = generate_eval(text, 1, 3000, logger)
            if len(eval_pair) == 0:    
                # Error in eval generation
                continue
            else:
                # This returns a list, so we unpack to dict
                eval_pair = eval_pair[0]
        
        # Run eval 
        graded_answers, graded_retrieval, latency, predictions = run_eval(qa_chain, retriever, eval_pair, grade_prompt, retriever_type, num_neighbors, logger)
        
        # Assemble output 
        d = pd.DataFrame(predictions)
        d['answerScore'] = [g['text'] for g in graded_answers]
        d['retrievalScore'] = [g['text'] for g in graded_retrieval]
        d['latency'] = latency
        
        # Summary statistics
        d['answerScore'] = [1 if "INCORRECT" not in text else 0 for text in d['answerScore']]
        d['retrievalScore'] = [1 if "Context is relevant: True" in text else 0 for text in d['retrievalScore']]

        # Convert dataframe to dict
        d_dict = d.to_dict('records')
        if len(d_dict) == 1:
            yield json.dumps({"data":  d_dict[0]})
        else:
            logger.warn("A QA pair was not evaluated correctly. Skipping this pair.")

@app.post("/evaluator-stream")
async def create_response(
    files: List[UploadFile] = File(...),
    num_eval_questions: int = Form(5),
    chunk_chars: int = Form(1000),
    overlap: int = Form(100),
    split_method: str = Form("RecursiveTextSplitter"),
    retriever_type: str = Form("similarity-search"),
    embeddings: str = Form("OpenAI"),
    model_version: str = Form("gpt-3.5-turbo"),
    grade_prompt: str = Form("Fast"),
    num_neighbors: int = Form(3),
    test_dataset: str = Form("[]"),
):
    test_dataset = json.loads(test_dataset)
    return EventSourceResponse(run_evaluator(files, num_eval_questions, chunk_chars,
                                             overlap, split_method, retriever_type, embeddings, model_version,grade_prompt,num_neighbors,test_dataset), headers={"Content-Type": "text/event-stream", "Connection": "keep-alive", "Cache-Control": "no-cache"})
                                             