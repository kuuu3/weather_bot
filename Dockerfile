FROM python:3.12-slim

WORKDIR /app

COPY weather_monitor.py README.md requirements.txt ./

CMD ["python", "weather_monitor.py"]
