import os
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field

# Загружаем переменные из .env
load_dotenv()
gemini_api_key = os.getenv("GEMINI_API_KEY")

# Инициализируем новый клиент Google GenAI
client = genai.Client(api_key=gemini_api_key)

# Таймаут одного запроса к модели (мс). При истечении — переход к следующей модели в очереди.
REQUEST_TIMEOUT_MS = 10_000

# Описываем схему ответа
class FoodAnalysis(BaseModel):
    dish: str = Field(description="Название распознанного блюда")
    calories: int = Field(description="Калорийность в ккал")
    proteins: float = Field(description="Белки в граммах")
    fats: float = Field(description="Жиры в граммах")
    carbs: float = Field(description="Углеводы в граммах")

# Анализ локального фото через Gemini: загружает файл, перебирает models_queue
# с таймаутом 10 с на запрос; при ошибке/таймауте пропускает оставшиеся
# вхождения той же модели и пробует следующую. Используется как точка входа скрипта.
def analyze_food_local_photo(image_path):
    # 1. Загружаем файл через Files API
    print("Загрузка файла в Google Files API...")
    uploaded_file = client.files.upload(file=image_path)
    print(f"Файл успешно загружен. URI: {uploaded_file.uri}")
    
    prompt = "Распознай еду на фото и детально оцени её калорийность и БЖУ."
    
    # Очередь моделей для попыток (всего 4 попытки)
    # Актуальная очередь моделей для попыток (середина 2026 года)
    models_queue = [
        "gemini-3.5-flash",          # Попытка 1: Основной рабочий инструмент (быстрый и умный)
        "gemini-flash-latest",       # Попытка 2: Алияс (сейчас указывает на gemini-3-flash-preview)
        "gemini-3.1-flash-lite",     # Попытка 3: Легковесная альтернатива текущего поколения
        "gemini-2.5-flash"           # Попытка 4: Стабильный legacy-вариант (работает до октября 2026)
    ]
    
    response_text = None
    skipped_models = set()
    total_attempts = len(models_queue)

    # Проходим по нашей очереди моделей
    for index, model_name in enumerate(models_queue, start=1):
        # Если эта модель уже не ответила вовремя/упала — не тратим на неё ещё попытки
        if model_name in skipped_models:
            print(f"\n[Попытка {index}/{total_attempts}] Пропуск {model_name}: "
                  f"уже не ответила в лимите {REQUEST_TIMEOUT_MS // 1000} с.")
            continue

        try:
            print(f"\n[Попытка {index}/{total_attempts}] Отправка запроса к модели: {model_name} "
                  f"(таймаут {REQUEST_TIMEOUT_MS // 1000} с)...")
            
            response = client.models.generate_content(
                model=model_name,
                contents=[uploaded_file, prompt],
                config={
                    'response_mime_type': 'application/json',
                    'response_schema': FoodAnalysis,
                    # Таймаут на HTTP-запрос; retries отключаем — fallback делает очередь моделей
                    'http_options': {
                        'timeout': REQUEST_TIMEOUT_MS,
                        'retry_options': {'attempts': 1},
                    },
                }
            )
            
            # Если запрос прошел успешно, сохраняем ответ и прерываем цикл
            response_text = response.text
            print(f"Успешно получено на модели {model_name}!")
            break
            
        except Exception as e:
            error_str = str(e)
            print(f"Попытка {index} не удалась. Ошибка: {error_str}")
            # Больше не пробуем эту же модель в оставшейся очереди
            skipped_models.add(model_name)
    
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
