IMAGE_NAME := korail-kakao-macro

help:           ## Show this help.
	@fgrep -h "##" $(MAKEFILE_LIST) | fgrep -v fgrep | sed -e 's/\\$$//' | sed -e 's/##//'

.PHONY: setup
setup:			## Setup development environment (install pipenv if needed)
	@command -v pipenv >/dev/null 2>&1 || { echo "Installing pipenv..."; \
		if command -v brew >/dev/null 2>&1; then brew install pipenv; else python3 -m pip install --user pipenv; fi; }

.PHONY: install
install:		## Install dependencies with pipenv
	pipenv install --dev

.PHONY: kakao-auth
kakao-auth:		## Get the KakaoTalk token (once) and send a test message
	pipenv run python src/kakao_auth.py

.PHONY: kakao-test
kakao-test:		## Send a KakaoTalk test message with the saved token
	pipenv run python src/kakao_auth.py --test

.PHONY: check
check:			## Validate .env settings without searching
	pipenv run python src/macro.py --dry-run

.PHONY: run
run:			## Run the reservation macro (settings from .env)
	pipenv run python src/macro.py

.PHONY: shell
shell:			## Open pipenv shell
	pipenv shell

.PHONY: requirements
requirements:	## Generate requirements.txt from Pipfile.lock (for Docker)
	pipenv requirements > requirements.txt

.PHONY: test
test:			## Run all tests with pytest
	pipenv run pytest tests/ -v

.PHONY: build
build:			## Build Docker Image
	docker build -t ${IMAGE_NAME} .
