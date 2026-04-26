SHELL := /bin/bash

STAGE ?=
STACK_NAME := solo-vault-shared-network-$(STAGE)

.PHONY: help install deploy destroy ensure-stage deploy-pipeline destroy-pipeline upload-dataset

help:
	@echo "Usage:"
	@echo "  make install"
	@echo "  make deploy STAGE=dev|staging"
	@echo "  make destroy STAGE=dev|staging"

ensure-stage:
	@if [ -z "$(STAGE)" ]; then \
		echo "Error: STAGE is required. Use STAGE=dev or STAGE=staging."; \
		exit 1; \
	fi
	@if [ "$(STAGE)" != "dev" ] && [ "$(STAGE)" != "staging" ]; then \
		echo "Error: invalid STAGE '$(STAGE)'. Allowed values: dev, staging."; \
		exit 1; \
	fi

install:
	npm install

deploy: ensure-stage
	npm run iac -- --env $(STAGE)

destroy: ensure-stage
	npm run iac -- --action destroy --env $(STAGE) --confirm-destroy $(STACK_NAME)

## ── Pipeline (Engineer 3) ──────────────────────────────────────────

deploy-pipeline: ensure-stage
	@echo "Deploying full indexing pipeline ($(STAGE))..."
	bash infra/scripts/deploy-all-pipeline.sh $(STAGE)

destroy-pipeline: ensure-stage
	npm run iac:pipeline -- --action destroy --env $(STAGE) --confirm-destroy solo-vault

upload-dataset: ensure-stage
	python services/indexer/scripts/upload_dataset.py \
		--bucket solo-vault-vault-$(STAGE) --no-endpoint
