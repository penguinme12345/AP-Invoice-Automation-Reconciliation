.PHONY: install demo test run report clean

install:
	pip install -r requirements.txt

demo:
	python -m ap_automation demo

test:
	python -m pytest

run:
	python -m ap_automation run

report:
	python -m ap_automation report

clean:
	rm -rf data outputs .pytest_cache
