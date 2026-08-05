.PHONY: help images-build images-push images-lock images-pull test test-fast check

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

images-build:  ## Build every image declared with source: build
	python tools/images.py build $(if $(SCOPE),--scope $(SCOPE),)

images-push:   ## Push every built image to the registry
	python tools/images.py push $(if $(SCOPE),--scope $(SCOPE),)

images-lock:   ## Resolve digests and write images.lock.json
	python tools/images.py lock

images-pull:   ## Pull every image at its locked digest
	python tools/images.py pull

test:          ## Full suite, including container-backed runner tests
	docker pull bash:5.2
	pytest tests/ -v

test-fast:     ## Skip anything that needs Docker
	pytest tests/ -q -m "not docker"

check:         ## Contract and manifest validation only
	pytest tests/test_all_pipelines.py tests/test_manifest.py tests/test_images.py -q
