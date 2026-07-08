import requests
import base64
import os
from dotenv import load_dotenv
import google.generativeai as genai
import urllib.request

# Загружаем переменные из файла .env в окружение
load_dotenv()

# Получаем значения переменных
gemini_api_key = os.getenv("GEMINI_API_KEY")

# Настраиваем ключ
genai.configure(api_key=gemini_api_key)


# def analyze_food_by_url(image_url):
#     # 1. Скачиваем картинку во временный файл
#     image_path = "temp_food_image.jpg"
#     urllib.request.urlretrieve(image_url, image_path)

def analyze_food_by_url(image_path):
    
    # 2. Загружаем файл в Google Files API
    # Он загрузится на сервера Google и отдаст специальный внутренний URI
    uploaded_file = genai.upload_file(path=image_path)
    print(f"Файл успешно загружен в Google. URI: {uploaded_file.uri}")
    
    # 3. Делаем запрос к Gemini
    model = genai.GenerativeModel(model_name="gemini-flash-latest")
    
    prompt = (
        "Распознай еду на фото. Оцени калорийность и БЖУ. "
        # "Ответь строго в формате JSON: "
        # "{'dish': 'название', 'calories': 0, 'proteins': 0, 'fats': 0, 'carbs': 0}"
    )
    
    # Передаем и загруженный файл (через его URI), и текст
    response = model.generate_content([uploaded_file, prompt])
    
    # 4. Удаляем файл из хранилища Google после анализа (чтобы не забивать лимиты)
    genai.delete_file(uploaded_file.name)
    
    return response.text

# Пример вызова (ссылка на фото, полученная, например, из Telegram)
# url = "https://example.com/user_dinner.jpg"
# print(analyze_food_by_url(url))

photo_path = "img_1.jpg" # Будет работать только если запуск всегда из корня проекта

result_analyze_food_photo = analyze_food_by_url(photo_path)
print("result_analyze_food_photo: ")
print(result_analyze_food_photo)

# print(analyze_food_photo("plate.jpg", key))