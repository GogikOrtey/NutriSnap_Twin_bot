import os
import time
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field

# Загружаем переменные из .env
load_dotenv()
gemini_api_key = os.getenv("GEMINI_API_KEY")

# Инициализируем новый клиент Google GenAI
client = genai.Client(api_key=gemini_api_key)

# Описываем схему ответа
class FoodAnalysis(BaseModel):
    dish: str = Field(description="Название распознанного блюда")
    calories: int = Field(description="Калорийность в ккал")
    proteins: float = Field(description="Белки в граммах")
    fats: float = Field(description="Жиры в граммах")
    carbs: float = Field(description="Углеводы в граммах")

def analyze_food_local_photo(image_path):
    # 1. Загружаем файл через Files API
    print("Загрузка файла в Google Files API...")
    uploaded_file = client.files.upload(file=image_path)
    print(f"Файл успешно загружен. URI: {uploaded_file.uri}")
    
    prompt = "Распознай еду на фото и детально оцени её калорийность и БЖУ."
    
    # Очередь моделей для попыток (всего 4 попытки)
    models_queue = [
        "gemini-flash-latest",  # Попытка 1
        "gemini-flash-latest",  # Попытка 2
        "gemini-2.5-flash",     # Попытка 3
        "gemini-1.5-flash"      # Попытка 4
    ]
    
    response_text = None

    # Проходим по нашей очереди моделей
    for index, model_name in enumerate(models_queue, start=1):
        try:
            print(f"\n[Попытка {index}/4] Отправка запроса к модели: {model_name}...")
            
            response = client.models.generate_content(
                model=model_name,
                contents=[uploaded_file, prompt],
                config={
                    'response_mime_type': 'application/json',
                    'response_schema': FoodAnalysis,
                }
            )
            
            # Если запрос прошел успешно, сохраняем ответ и прерываем цикл
            response_text = response.text
            print(f"Успешно получено на модели {model_name}!")
            break
            
        except Exception as e:
            error_str = str(e)
            print(f"Попытка {index} не удалась. Ошибка: {error_str}")
            
            # Если это последняя попытка, то засыпать уже не нужно
            if index < len(models_queue):
                print("Ждем 3 секунды перед следующей попыткой...")
                time.sleep(3)
    
    # 3. Чистим за собой файл на серверах Google в любом случае
    print("\nУдаление временного файла из Google Cloud...")
    client.files.delete(name=uploaded_file.name)
    
    return response_text

# Точка входа
if __name__ == "__main__":
    photo_path = "img_1.jpg"
    
    if not os.path.exists(photo_path):
        print(f"Ошибка: Файл {photo_path} не найден в корне проекта!")
    else:
        result = analyze_food_local_photo(photo_path)
        if result:
            print("\nИтоговый ответ от Gemini (JSON):")
            print(result)
        else:
            print("\nК сожалению, все 4 попытки завершились ошибкой из-за перегрузки серверов.")