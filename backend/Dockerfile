FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml ./
COPY tradelab ./tradelab
RUN pip install --no-cache-dir .
# Optional multi-agent strategy (TradingAgents, Apache-2.0, pinned tag). Heavy: off by default.
ARG INSTALL_TRADINGAGENTS=0
RUN if [ "$INSTALL_TRADINGAGENTS" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* && \
      pip install --no-cache-dir "git+https://github.com/TauricResearch/TradingAgents@v0.7.1"; fi
ENV DB_PATH=/data/tradelab.sqlite
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"
CMD ["uvicorn", "tradelab.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
