REM create an use a virtual env (venv) - optional
REM python -m pip install pillow diffusers loguru
python -m pip install torch==2.5.1+cu124 torchvision==0.20.1+cu124 torchaudio==2.5.1+cu124 --index-url https://download.pytorch.org/whl/cu124
pip install virtualenv
python -m venv .venv

REM continue at Venv
.\.venv\Scripts\activate
pip install -r requirements.txt