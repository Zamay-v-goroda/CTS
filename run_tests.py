import os
import subprocess
import time
import argparse

def run_team_pipeline():
    parser = argparse.ArgumentParser(description="CTS Pipeline Runner")
    parser.add_argument(
        "--case", 
        type=str, 
        default=None, 
        help="Имя конкретного json-файла для теста (например, test_easy.json)."
    )
    args = parser.parse_args()

    tests_dir = "tests"
    results_dir = "results"

    if args.case:
        test_cases = [args.case]
    else:
        if os.path.exists(tests_dir):
            test_cases = [f for f in os.listdir(tests_dir) if f.endswith(".json")]
            test_cases.sort()
        else:
            test_cases = []

    print("=" * 60)
    print("ЗАПУСК КОМАНДНОГО ТЕСТИРОВАНИЯ CTS ПРОЕКТА")
    print("=" * 60)
    print(f"Найдено файлов для обработки: {len(test_cases)}")

    if not test_cases:
        print(f"[Ошибка] Не найдено .json конфигураций в папке {tests_dir}!")
        return

    # Папка результатов НЕ очищается: каждый прогон сохраняется в своей подпапке
    # (results/run_<timestamp>_<case>), чтобы можно было сравнивать запуски.
    os.makedirs(results_dir, exist_ok=True)

    print("\n[ЗАПУСК] Прогон конфигураций через компилятор...")
    
    for case in test_cases:
        source_path = os.path.join(tests_dir, case)
        
        if not os.path.exists(source_path):
            print(f"Файл {source_path} не найден. Пропуск.")
            continue
            
        print(f"\nТестируем конфигурацию: {case}...")
        case_name = case.replace(".json", "") # Выделит "test_easy", "test_medium" и т.д.
        
        start = time.time()
        result = subprocess.run([
            "python", "source/main.py", 
            "--input", source_path, 
            "--output_dir", results_dir,
            "--name", case_name
        ], capture_output=True, text=True)
        
        duration = time.time() - start
            
        if result.returncode == 0:
            print(f"-> УСПЕХ: {case} обработан за {duration:.4f} сек.")
        else:
            print(f"-> ОШИБКА при обработке {case}! См. логи верификации.")
            print(result.stderr)

    print("\n" + "=" * 60)
    print(f"Тестирование завершено! Результаты прогонов: /{results_dir}/run_<время>_<case>/")
    print(f"Ссылка на последний прогон: /{results_dir}/latest")
    print("=" * 60)

if __name__ == "__main__":
    run_team_pipeline()
