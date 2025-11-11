# Użyj oficjalnego, lekkiego obrazu Python
FROM python:3.11-slim

# Ustaw folder roboczy w kontenerze
WORKDIR /app

# Skopiuj plik zależności i zainstaluj je
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Skopiuj resztę kodu aplikacji
COPY . .

# Uruchom bota
CMD ["python", "bota.py"]
