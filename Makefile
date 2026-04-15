SHELL := /bin/bash

STAGE ?=
STACK ?= shared-network
STACK_NAME := solo-vault-$(STACK)-$(STAGE)

.PHONY: help install deploy destroy ensure-args

help:
	@echo "Usage:"
	@echo "  make install"
	@echo "  make deploy  STAGE=dev|staging [STACK=shared-network|secrets]"
	@echo "  make destroy STAGE=dev|staging [STACK=shared-network|secrets]"
	@echo ""
	@echo "STACK defaults to shared-network."

ensure-args:
	@if [ -z "$(STAGE)" ]; then \
		echo "Error: STAGE is required. Use STAGE=dev or STAGE=staging."; \
		exit 1; \
	fi
	@if [ "$(STAGE)" != "dev" ] && [ "$(STAGE)" != "staging" ]; then \
		echo "Error: invalid STAGE '$(STAGE)'. Allowed values: dev, staging."; \
		exit 1; \
	fi
	@if [ "$(STACK)" != "shared-network" ] && [ "$(STACK)" != "secrets" ]; then \
		echo "Error: invalid STACK '$(STACK)'. Allowed values: shared-network, secrets."; \
		exit 1; \
	fi

install:
	npm install

deploy: ensure-args
	npm run iac -- --env $(STAGE) --stack $(STACK)

destroy: ensure-args
	npm run iac -- --action destroy --env $(STAGE) --stack $(STACK) --confirm-destroy $(STACK_NAME)
