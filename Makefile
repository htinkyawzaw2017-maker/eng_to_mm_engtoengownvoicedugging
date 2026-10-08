# Common checks. The unit tests and the local end-to-end run need no AWS
# account, no FFmpeg and no network.
PY ?= python

.PHONY: install test e2e fmt fmt-check tf-validate build api worker clean

install:
	$(PY) -m pip install -r requirements.txt
	$(PY) -m pip install -r requirements-dev.txt

test:
	$(PY) -m unittest discover -s tests

e2e:
	$(PY) scripts/e2e_local.py

fmt:
	terraform fmt -recursive infra/terraform

fmt-check:
	terraform fmt -check -recursive infra/terraform

tf-validate:
	cd infra/terraform && terraform init -backend=false && terraform validate

build:
	docker build -t recap:latest .

api:
	$(PY) -m uvicorn cloud.api:app --host 0.0.0.0 --port 8000

worker:
	$(PY) cloud/aws_worker.py

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache
