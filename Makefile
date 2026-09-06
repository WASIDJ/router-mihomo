.PHONY: build deploy verify clean

build:
	python3 build.py

deploy:
	python3 deploy.py

verify:
	python3 verify.py && python3 verify_transparent.py

clean:
	rm -rf build/
