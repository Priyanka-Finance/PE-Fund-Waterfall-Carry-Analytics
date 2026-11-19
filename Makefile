.PHONY: install test lint format run compare all excel verify-excel app readme clean

install:          ## install the package with every extra
	pip install -e ".[all]"

test:             ## run the test suite with coverage
	pytest --cov=pefund --cov-report=term-missing

lint:             ## static checks
	ruff check .
	ruff format --check .

format:           ## apply formatting and safe fixes
	ruff check --fix .
	ruff format .

run:              ## headline results for the base case
	pefund run

compare:          ## whole-of-fund against deal-by-deal
	pefund compare

all:              ## every table, chart, the Excel model and the commentary
	pefund all

excel:            ## build only the Excel model
	pefund excel --out excel/PE_Fund_Waterfall_Model.xlsx

verify-excel:     ## recalculate the workbook with LibreOffice and read its Checks sheet
	pefund verify-excel excel/PE_Fund_Waterfall_Model.xlsx

app:              ## interactive dashboard
	streamlit run app/streamlit_app.py

readme:           ## refresh the results block in README.md
	python scripts/update_readme.py

clean:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov build dist src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
