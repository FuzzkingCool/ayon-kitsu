include .env

AYON_ADDON_VERSION=$(shell python -c "import os;import sys;content={};f=open('$(CURDIR)/../../package.py');exec(f.read(),content);f.close();print(content['version'])")
AYON_ADDON_NAME=kitsu
SERVICE_NAME=processor
BASE_NAME := ayon-$(AYON_ADDON_NAME)-$(SERVICE_NAME)
REGISTRY ?= ghcr.io
NAMESPACE ?= ${AYON_STUDIO_NAME}
IMAGE_NAME := $(NAMESPACE)/kitsu-processor-docker
IMAGE := $(REGISTRY)/$(IMAGE_NAME):$(AYON_ADDON_VERSION)
IMAGE_LATEST := $(REGISTRY)/$(IMAGE_NAME):latest
default:
	@echo * *************************
	@echo * AYON Kitsu $(AYON_ADDON_VERSION) $(SERVICE_NAME) Service Builder
	@echo * Docker image name: $(IMAGE)
	@echo * *************************
	@echo * 
	@echo * Usage: make [target]
	@echo *  
	@echo * Targets:
	@echo *   run          Run service without building.
	@echo *   build        Build docker image.
	@echo *   clean        Remove local images.
	@echo *   dev          Run a service locally
	@echo *   dist         Upload the image to a registry

export AYON_ADDON_VERSION
export AYON_ADDON_NAME
export AYON_API_KEY
export AYON_SERVER_URL

run:
	poetry run python -m processor

build:
	docker build -t $(IMAGE) -t $(IMAGE_LATEST) -f Dockerfile .

clean:
	docker rmi $(IMAGE)

clean-build: clean
	build

dist: build
	docker push $(IMAGE)
	docker push $(IMAGE_LATEST)

dist-ghcr: build
	@echo "Pushing to GitHub Container Registry..."
	@echo "Make sure you're authenticated: docker login ghcr.io -u USERNAME --password-stdin"
	docker push $(IMAGE)
	docker push $(IMAGE_LATEST)

dev: build
	docker run --rm -u ayonuser -ti \
		--hostname kitsu-dev-worker \
		--env AYON_API_KEY=${AYON_API_KEY} \
		--env AYON_SERVER_URL=${AYON_SERVER_URL} \
		--env AYON_ADDON_NAME=${AYON_ADDON_NAME} \
		--env AYON_ADDON_VERSION=$(AYON_ADDON_VERSION) \
		$(IMAGE) python -m processor
