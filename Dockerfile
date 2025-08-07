# Gunakan image Python 3.10 slim sebagai base image
FROM python:3.10-slim

# Set direktori kerja di dalam container
WORKDIR /app

# Salin semua file dari proyek lokal ke dalam container
COPY . .

# Install dependensi Python dari requirements.txt
# Ini akan membuat venv di dalam container dan menginstalnya
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip && \
    /opt/venv/bin/pip install -r requirements.txt

# Definisikan perintah untuk menjalankan aplikasi
# Ini akan menjalankan main.py menggunakan python dari venv yang sudah dibuat
CMD ["/opt/venv/bin/python", "main.py"]