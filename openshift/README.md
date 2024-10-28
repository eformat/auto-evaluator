# openshift deployment

Build images in OpenShift

```bash
# api
oc -n openshift new-build \
  --strategy docker --dockerfile - --name auto-eval-api < Containerfile.api

# nextjs
oc -n openshift new-build \
  --strategy docker --dockerfile - --name auto-eval-nextjs < Containerfile.nextjs
```

Deploy

```bash
oc new-project auto-eval
oc apply -k .
```
