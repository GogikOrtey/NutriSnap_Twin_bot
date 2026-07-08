import os
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field

# Загружаем переменные из .env
load_dotenv()
gemini_api_key = os.getenv("GEMINI_API_KEY")

# Инициализируем новый клиент Google GenAI
client = genai.Client(api_key=gemini_api_key)

# Описываем схему ответа, которую хотим получить от ИИ
class FoodAnalysis(BaseModel):
    dish: str = Field(description="Название распознанного блюда")
    calories: int = Field(description="Калорийность в ккал")
    proteins: float = Field(description="Белки в граммах")
    fats: float = Field(description="Жиры в граммах")
    carbs: float = Field(description="Углеводы в граммах")

def analyze_food_local_photo(image_path):
    # 1. Загружаем файл через Files API нового SDK
    print("Загрузка файла в Google Files API...")
    uploaded_file = client.files.upload(file=image_path)
    print(f"Файл успешно загружен. URI: {uploaded_file.uri}")
    
    prompt = "Распознай еду на фото и детально оцени её калорийность и БЖУ."
    
    try:
        # 2. Делаем запрос к актуальной модели с требованием вернуть структурированный JSON
        print("Ожидание ответа от Gemini...")
        response = client.models.generate_content(
            model='gemini-flash-latest', # Автоматически перенаправит на актуальную версию Flash
            contents=[uploaded_file, prompt],
            config={
                'response_mime_type': 'application/json',
                'response_schema': FoodAnalysis, # Модель железно ответит по этой схеме
            }
        )
        
        # 3. Чистим за собой файл на серверах Google
        client.files.delete(name=uploaded_file.name)
        
        return response.text

    except Exception as e:
        # На случай, если ключ все еще не работает, поймаем ошибку здесь
        print(f"Произошла ошибка при запросе: {e}")
        # Пытаемся удалить файл даже при ошибке
        client.files.delete(name=uploaded_file.name)
        return None

# Точка входа
if __name__ == "__main__":
    photo_path = "img_1.jpg"
    
    if not os.path.exists(photo_path):
        print(f"Ошибка: Файл {photo_path} не найден в корне проекта!")
    else:
        result = analyze_food_local_photo(photo_path)
        if result:
            print("\nУспешный ответ от Gemini (JSON):")
            print(result)