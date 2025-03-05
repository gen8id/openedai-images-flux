REM create an use a virtual env (venv) - optional
pip install virtualenv
python -m pip install -r requirements.txt
python -m venv .venv

REM continue at Venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt