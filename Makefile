.PHONY: run test check check-docker
run:
	PYTHONPATH=src python3 -m solve2ship
test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v
	node --test tests/web.test.mjs
check: test
	python3 scripts/check_repository.py --public
check-docker:
	python3 scripts/verify_docker.py
