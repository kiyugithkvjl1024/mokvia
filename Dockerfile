FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MOKVIA_DATA_ROOT=/data
WORKDIR /app
COPY . /app
RUN mkdir -p /data && useradd --uid 10001 --create-home mokvia && chown mokvia:mokvia /data
USER 10001:10001
EXPOSE 24873
CMD ["python3", "-m", "local_runtime", "serve", "--container"]
