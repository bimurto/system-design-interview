DOCKER_IMAGE_NAME = claude-sandbox
DOCKER_CONTAINER_NAME = claude-session

.PHONY: build run stop shell restart clean logs status

build:
	docker build -f Dockerfile -t $(DOCKER_IMAGE_NAME) .

run:
	docker run -it \
		-v $(CURDIR):/workspace \
		-v $(HOME)/.claude-docker-config/:/home/claude \
		--name $(DOCKER_CONTAINER_NAME) \
		$(DOCKER_IMAGE_NAME)

run-api:
	docker run -it \
		-v $(CURDIR):/workspace \
		-v $(HOME)/.claude-docker-config/:/home/claude \
		--name $(DOCKER_CONTAINER_NAME) \
		$(DOCKER_IMAGE_NAME)

stop:
	docker stop $(DOCKER_CONTAINER_NAME) || true
	docker rm $(DOCKER_CONTAINER_NAME) || true

shell:
	docker exec -it $(DOCKER_CONTAINER_NAME) bash

restart: stop run

clean: stop
	docker rmi $(DOCKER_IMAGE_NAME) || true

logs:
	docker logs -f $(DOCKER_CONTAINER_NAME)

status:
	@docker ps -a --filter name=$(DOCKER_CONTAINER_NAME)