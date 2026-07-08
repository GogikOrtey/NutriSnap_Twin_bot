import requests
import base64
import os
from dotenv import load_dotenv

# Загружаем переменные из файла .env в окружение
load_dotenv()

# Получаем значения переменных
gemini_api_key = os.getenv("GEMINI_API_KEY")

print(gemini_api_key)

def analyze_food_photo(photo_path): 
    # 1. Читаем картинку и переводим в Base64
    with open(photo_path, "rb") as image_file:
        base64_image = base64.b64encode(image_file.read()).decode('utf-8')
    
    # 2. Формируем URL и заголовки
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_api_key}"
    headers = {"Content-Type": "application/json"}
    
    # 3. Пишем системный промпт
    prompt = (
        "Распознай еду на фото. Верни ответ СТРОГО в формате JSON. "
        "Структура: {'dish': 'название', 'calories': 0, 'proteins': 0, 'fats': 0, 'carbs': 0}. "
        "Не добавляй никакой разметки markdown (типа ```json), только чистый текст JSON."
    )
    
    # 4. Собираем тело запроса
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {
                    "inlineData": {
                        "mimeType": "image/jpeg",  # укажи image/png, если формат PNG
                        "data": base64_image
                    }
                }
            ]
        }]
    }
    
    # 5. Делаем запрос
    response = requests.post(url, json=payload, headers=headers)
    
    if response.status_code == 200:
        result = response.json()
        # Извлекаем текстовый ответ из структуры ответа Gemini
        text_response = result['candidates'][0]['content']['parts'][0]['text']
        return text_response
    else:
        return f"Ошибка API: {response.status_code} - {response.text}"


photo_path = "img_1.jpg" # Будет работать только если запуск всегда из корня проекта

result_analyze_food_photo = analyze_food_photo(photo_path)

# print(analyze_food_photo("plate.jpg", key))