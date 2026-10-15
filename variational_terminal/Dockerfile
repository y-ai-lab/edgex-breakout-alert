FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt /app/root-requirements.txt
COPY analysis_terminal/requirements.txt /app/analysis-requirements.txt
RUN pip install --no-cache-dir -r /app/root-requirements.txt -r /app/analysis-requirements.txt
COPY app.py /app/app.py
COPY analysis_terminal /app/analysis_terminal
COPY variational_terminal /app/variational_terminal
ENV PYTHONUNBUFFERED=1 EDGEX_EXEC_MODE=OFF
CMD ["python", "-m", "variational_terminal.server"]
