# 로또청약 알리미 서버 (Cloud Run Job). 배포 방법은 server/README.md
FROM python:3.12-slim

# 공고 일정·조용한 시간 계산은 한국 시간 기준
ENV TZ=Asia/Seoul PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server/requirements.txt server/
RUN pip install --no-cache-dir -r server/requirements.txt

# PC 프로그램과 함께 쓰는 모듈 + 서버 코드
COPY price.py eligibility.py myhome.py ./
COPY server/*.py server/

CMD ["python", "-m", "server.collector"]
