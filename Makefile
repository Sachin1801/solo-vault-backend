SHELL := /bin/bash

STAGE ?=
STACK_NAME := solo-vault-shared-network-$(STAGE)

.PHONY: help install deploy destroy ensure-stage

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
