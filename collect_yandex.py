"""Сбор текстов и оценок из публичных страниц организаций на Яндекс Картах."""

# Подключаем разбор параметров запуска.
import argparse

# Подключаем вычисление ключей SHA-256.
import hashlib

# Подключаем чтение и запись JSON.
import json

# Подключаем проверку адресов и текстовых признаков.
import re

# Подключаем измерение времени и паузы.
import time

# Подключаем дату и время в UTC.
from datetime import datetime, timezone

# Подключаем разбор HTTP-даты.
from email.utils import parsedate_to_datetime

# Подключаем работу с путями и файлами.
from pathlib import Path

# Подключаем разбор адресов страниц.
from urllib.parse import urlsplit

# Подключаем playwright.sync_api.
from playwright.sync_api import TimeoutError as BrowserTimeout, sync_playwright

# Подключаем чтение встроенного JSON из HTML.
from extract_saved_html import extract as extract_saved_html


# Задаём селектор контейнера отдельного отзыва.
CARDS = ".business-reviews-card-view__review"
# Задаём версию формата сохранённых результатов.
SCHEMA_VERSION = 1
# Описываем чтение видимых отзывов внутри браузера.
EXTRACT = r"""limit => {
  // Читаем текст найденного элемента; отсутствующее значение обозначаем null.
  const read = (root, selector) => root.querySelector(selector)?.textContent?.trim() || null;
  // Читаем атрибут content, которым размечены оценки и даты.
  const content = (root, selector) => root.querySelector(selector)?.getAttribute('content') || null;
  // Обрабатываем карточки отзывов в пределах указанного лимита.
  const reviews = [...document.querySelectorAll('.business-reviews-card-view__review')].slice(0, limit).map(card => {
    // Ищем текст только внутри текущего отзыва.
    const body = card.querySelector('[itemprop="reviewBody"], .business-review-view__body');
    // Находим контейнер текста, отделённый от кнопки раскрытия.
    const textNode = body?.querySelector('.spoiler-view__text-container');
    // Читаем текст и убираем пробелы по краям.
    let text = textNode?.textContent?.trim() || null;
    // Если отдельного контейнера нет, читаем копию тела отзыва.
    if (!text && body) {
      // Копируем DOM-узел, сохраняя исходную страницу без изменений.
      const copy = body.cloneNode(true);
      // Удаляем подписи кнопок из копии текста.
      copy.querySelectorAll('.spoiler-view__button, .business-review-view__expand').forEach(e=>e.remove());
      // Читаем очищенный текст из копии узла.
      text = copy.textContent.trim();
    }
    // Ищем оценку, размеченную внутри отдельного отзыва.
    const ratingRaw = content(card, '[itemprop="reviewRating"] [itemprop="ratingValue"]')
      // При необходимости пробуем запасной селектор оценки.
      || content(card, 'meta[itemprop="ratingValue"]');
    // Преобразуем оценку в число; отсутствие оставляем null.
    let rating = ratingRaw === null ? null : Number(ratingRaw);
    // Если числовой оценки нет, проверяем нарисованные звёзды.
    if (rating === null) {
      // Находим звёзды только в блоке оценки текущего отзыва.
      const stars = card.querySelectorAll('.business-review-view__rating .business-rating-badge-view__star');
      // При наличии пяти звёзд считаем заполненные.
      if (stars.length === 5) rating = [...stars].filter(e=>e.classList.contains('_full')).length;
    }
    // Отбрасываем дробные и выходящие за диапазон 1–5 значения.
    if (!Number.isInteger(rating) || rating < 1 || rating > 5) rating = null;
    // Возвращаем извлечённые поля.
    return {
      // Читаем отображаемое имя автора.
      author: read(card, '[itemprop="author"] [itemprop="name"]'),
      // Сохраняем публичную ссылку на профиль автора.
      author_url: card.querySelector('.business-review-view__link')?.getAttribute('href') || null,
      // Читаем дату из HTML-разметки отзыва.
      date: content(card, '[itemprop="datePublished"]'),
      // Передаём текст и индивидуальную оценку.
      text, rating,
      // Проверяем, остался ли текст свёрнутым.
      text_truncated: !!body?.querySelector('.spoiler-view__text._collapsed'),
    };
  });
  // Отдельно читаем общую оценку организации.
  const ratingText = read(document, '.business-summary-rating-badge-view__rating');
  // Приводим текст счётчика к числу, убирая пробелы.
  const number = text => { const m=text?.replace(/\s/g, '').match(/\d+/); return m ? Number(m[0]) : null; };
  // Возвращаем извлечённые поля.
  return {reviews, company: {
    // Читаем название организации.
    name: read(document, '.orgpage-header-view__header'),
    // Преобразуем общую оценку организации в число.
    rating: ratingText ? Number(ratingText.replace(/[^\d,.]/g, '').replace(',', '.')) : null,
    // Читаем число всех оценок, включая оценки без текста.
    ratings_count: number(read(document, '.business-summary-rating-badge-view__rating-count')),
    // Читаем число текстовых отзывов на карточке.
    reviews_count: number(read(document, '.business-reviews-card-view__title')),
  }};
}"""


# Проверяем ссылку и приводим её к странице отзывов организации.
def canonical_url(value):
    # При числовом вводе сразу строим ссылку на отзывы.
    if value.isdigit():
        # Возвращаем идентификатор и канонический адрес страницы отзывов.
        return value, f"https://yandex.ru/maps/org/-/{value}/reviews/"
    # Разбираем URL на схему, домен и путь.
    parsed = urlsplit(value)
    # Проверяем допустимость домена, протокола или типа HTTP-ответа.
    if parsed.scheme != "https" or parsed.hostname not in {"yandex.ru", "yandex.com"}:
        # Сообщаем об ошибке входных или сохранённых данных.
        raise ValueError("Нужна публичная HTTPS-ссылка на организацию в Яндекс Картах")
    # Ищем числовой идентификатор в пути карточки организации.
    match = re.search(r"/maps/org/(?:[^/]+/)?(\d+)(?:/|$)", parsed.path)
    # Отклоняем ссылку без идентификатора организации.
    if not match:
        # Сообщаем об ошибке входных или сохранённых данных.
        raise ValueError("В ссылке не найден числовой идентификатор организации")
    # Извлекаем идентификатор организации из ссылки.
    oid = match.group(1)
    # Возвращаем идентификатор и канонический адрес страницы отзывов.
    return oid, f"https://{parsed.hostname}/maps/org/-/{oid}/reviews/"


# Формируем устойчивый ключ для объединения HTML и DOM одного отзыва.
def review_key(oid, row):
    # Полный текст обновляет тот же отзыв, сохраняя его ключ.
    # Объединяем организацию, автора и дату для устойчивого ключа.
    identity = [oid, row.get("author_url") or row.get("author"), row.get("date")]
    # Проверяем наличие автора и даты для устойчивого ключа.
    if not identity[1] or not identity[2]:
        # При нехватке автора или даты используем текст как дополнительный признак.
        identity.append(row.get("text"))
    # Сообщаем, что условие готовности или наличия проверки не выполнено.
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


# Сохраняем JSON через временный файл в той же папке.
def save_result(target, result):
    # Выбираем путь временного файла рядом с итоговым JSON.
    temporary = target.with_suffix(".json.tmp")
    # Записываем JSON во временный файл с сохранением кириллицы.
    temporary.write_text(
        # Сохраняем кириллицу без преобразования в последовательности Unicode.
        json.dumps(result, ensure_ascii=False, indent=2),
        # Записываем файл в UTF-8 для сохранения кириллицы.
        encoding="utf-8",
    )
    # Заменяем итоговый файл только после успешной записи временного.
    temporary.replace(target)


# Переводим Retry-After из числа секунд или HTTP-даты в длительность ожидания.
def retry_seconds(value):
    # При отсутствии Retry-After дополнительное ожидание не задаётся.
    if not value:
        # Возвращаем неотрицательную длительность ожидания в секундах.
        return 0
    # Обрабатываем возможную ошибку следующего этапа.
    try:
        # Возвращаем неотрицательную длительность ожидания в секундах.
        return max(0, float(value))
    # Перехватываем указанную ошибку и сохраняем понятный итог.
    except ValueError:
        # Обрабатываем возможную ошибку следующего этапа.
        try:
            # Возвращаем неотрицательную длительность ожидания в секундах.
            return max(
                # Не допускаем отрицательное время ожидания.
                0,
                (
                    # Сравниваем серверную HTTP-дату с текущим временем UTC.
                    parsedate_to_datetime(value)
                    # Вычитаем текущее время UTC из серверной даты.
                    - datetime.now(timezone.utc)
                    # Переводим разность времени в секунды.
                ).total_seconds(),
            )
        # Перехватываем указанную ошибку и сохраняем понятный итог.
        except (ValueError, TypeError):
            # Возвращаем неотрицательную длительность ожидания в секундах.
            return 0


# Проверяем совместимость, полноту полей и достаточность сохранённого результата.
def cache_complete(cached, oid, limit):
    # Проверяем связанные условия перед продолжением обработки.
    if (
        # Проверяем идентификатор организации и версию формата.
        cached.get("organization_id") != oid
        # Проверяем идентификатор организации и версию формата.
        or cached.get("schema_version", 1) != SCHEMA_VERSION
    ):
        # Сообщаем, что условие готовности или наличия проверки не выполнено.
        return False
    # Подготавливаем список отзывов для обработки.
    reviews = cached.get("reviews", [])
    # Проверяем структуру и уникальность сохранённых отзывов.
    if not isinstance(reviews, list) or any(not isinstance(r, dict) for r in reviews):
        # Сообщаем, что условие готовности или наличия проверки не выполнено.
        return False
    # Проверяем структуру и уникальность сохранённых отзывов.
    if len({r.get("id") for r in reviews}) != len(reviews):
        # Сообщаем, что условие готовности или наличия проверки не выполнено.
        return False
    # Отклоняем кэш, если хотя бы у одного отзыва не хватает обязательного поля.
    if any(
        # Проверяем обязательное поле сохранённого отзыва.
        not r.get("id")
        # Текст отзыва должен быть непустым.
        or not r.get("text")
        # Дата отзыва должна присутствовать.
        or not r.get("date")
        # Проверяем обязательное поле сохранённого отзыва.
        or type(r.get("rating")) is not int
        # Допускаем только оценки от 1 до 5.
        or r["rating"] not in range(1, 6)
        # Проверяем обязательное поле сохранённого отзыва.
        or r.get("text_truncated") is not False
        # Последовательно обрабатываем записи указанного набора.
        for r in reviews
    ):
        # Сообщаем, что условие готовности или наличия проверки не выполнено.
        return False
    # Проверяем принадлежность и совместимость сохранённого результата.
    if cached.get("status") == "complete":
        # Проверяем, достаточно ли сохранённых отзывов для указанного запроса.
        return len(reviews) == cached.get("company", {}).get("reviews_count")
    # Проверяем, достаточно ли сохранённых отзывов для указанного запроса.
    return cached.get("status") == "capped" and len(reviews) >= limit


# Распознаём страницу проверки пользователя по адресу и видимому тексту.
def challenge(page):
    # Ищем признак CAPTCHA в адресе открытой страницы.
    if re.search(r"captcha|showcaptcha", page.url, re.I):
        # Подтверждаем найденный признак проверки пользователя.
        return True
    # Обрабатываем возможную ошибку следующего этапа.
    try:
        # Получаем текст страницы для поиска признаков проверки пользователя.
        text = page.locator("body").inner_text(timeout=3000)[:4000]
    # Перехватываем указанную ошибку и сохраняем понятный итог.
    except BrowserTimeout:
        # Сообщаем, что условие готовности или наличия проверки не выполнено.
        return False
    # Возвращаем признак обнаруженной проверки пользователя.
    return bool(
        # Ищем текстовый признак по регулярному выражению.
        re.search(
            # Ищем характерные сообщения о проверке пользователя или отказе в доступе.
            r"подтвердите.{0,50}(не робот|человек)|вы не робот|access denied",
            # Ищем признак проверки пользователя без учёта регистра.
            text,
            # Ищем признак проверки пользователя без учёта регистра.
            re.I,
        )
    )


# Собираем одну карточку, сохраняя прогресс и причину остановки.
def collect(page, oid, url, target, limit, scroll_pause):
    # Запоминаем монотонное время начала обработки карточки.
    started = time.monotonic()
    # Читаем сохранённый прогресс, если файл существует.
    previous = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    # Проверяем принадлежность и совместимость сохранённого результата.
    if previous and (
        # Проверяем идентификатор организации и версию формата.
        previous.get("organization_id") != oid
        # Проверяем идентификатор организации и версию формата.
        or previous.get("schema_version", 1) != SCHEMA_VERSION
    ):
        # Сообщаем об ошибке входных или сохранённых данных.
        raise ValueError(
            # Поясняем причину несовместимости сохранённого файла.
            "Сохранённый файл относится к другой организации или версии формата"
        )
    # Храним отзывы по уникальному ключу; при завершении оставляем подтверждённые в текущем DOM.
    rows = {r["id"]: r for r in previous.get("reviews", [])}
    # Учитываем отзывы, найденные в HTML или DOM при текущем запуске.
    observed = set()
    # Отдельно учитываем отзывы, действительно показанные текущей страницей.
    dom_observed = set()
    # Подготавливаем результат обработки и показатели полноты.
    result = {
        # Сохраняем версию формата JSON.
        "schema_version": SCHEMA_VERSION,
        # Сохраняем идентификатор организации.
        "organization_id": oid,
        # Сохраняем адрес страницы отзывов.
        "url": url,
        # Сохраняем состояние обработки.
        "status": "running",
        # Сохраняем время начала попытки в UTC.
        "collected_at": datetime.now(timezone.utc).isoformat(),
        # Сохраняем заданный предел числа отзывов.
        "limit": limit,
        # Сохраняем список отзывов.
        "reviews": list(rows.values()),
        # Сохраняем сведения об организации.
        "company": previous.get("company", {}).copy(),
        # Сохраняем счётчики запросов и HTTP-ошибки.
        "network": {"requests": 0, "blocked_media": 0, "issues": []},
        # Сохраняем число прокруток.
        "scrolls": 0,
        # Сохраняем число раскрытых текстов.
        "expanded_texts": 0,
    }
    # Храним причину остановки, обнаруженную при загрузке ресурсов.
    stopped = {}

    # Пропускаем необходимые ресурсы и отменяем загрузку изображений, шрифтов и медиа.
    def route_request(route):
        # Обновляем показатели загрузки ресурсов страницы.
        result["network"]["requests"] += 1
        # Исключаем ресурсы, не нужные для сбора текстов и оценок.
        if route.request.resource_type in {"image", "font", "media"}:
            # Обновляем показатели загрузки ресурсов страницы.
            result["network"]["blocked_media"] += 1
            # Отменяем загрузку ресурса, не нужного для текстов и оценок.
            route.abort()
        # Обрабатываем альтернативный исход предыдущей проверки.
        else:
            # Разрешаем загрузку ресурса, необходимого странице.
            route.continue_()

    # Учитываем HTTP-ошибки, читая только адрес, код и заголовки ответа.
    def observe_response(response):
        # Проверяем код ответа перед продолжением загрузки.
        if response.status < 400:
            # Завершаем этот этап без дополнительных действий.
            return
        # Разбираем URL на схему, домен и путь.
        parsed = urlsplit(response.url)
        # Обновляем показатели загрузки ресурсов страницы.
        result["network"]["issues"].append(
            {
                # Сохраняем состояние обработки.
                "status": response.status,
                # Сохраняем домен ответа.
                "host": parsed.hostname,
                # Сохраняем путь ответа без параметров.
                "path": parsed.path,
                # Сохраняем тип ресурса.
                "resource_type": response.request.resource_type,
            }
        )
        # Читаем только HTTP-статус; содержимое служебных ответов не используем.
        # Проверяем допустимость домена, протокола или типа HTTP-ответа.
        if parsed.hostname in {
            # Проверяем основной домен Яндекса.
            "yandex.ru",
            # Проверяем международный домен Яндекса.
            "yandex.com",
            # Учитываем ограничения для HTML-документа и динамических загрузок страницы.
        } and response.request.resource_type in {"document", "xhr", "fetch"}:
            # Проверяем код ответа перед продолжением загрузки.
            if response.status == 429:
                # Запоминаем ограничение доступа для завершения текущей попытки.
                stopped.update(
                    # Задаём код ответа или причину остановки.
                    status="rate_limited",
                    # Сохраняем серверную рекомендацию о длительности ожидания.
                    retry_after=response.headers.get("retry-after"),
                )
            # Проверяем код ответа перед продолжением загрузки.
            elif response.status in {401, 403}:
                # Запоминаем ограничение доступа для завершения текущей попытки.
                stopped.update(status="access_denied")

    # Пересчитываем показатели полноты и записываем текущие отзывы на диск.
    def checkpoint():
        # Обновляем список сохранённых отзывов.
        result["reviews"] = list(rows.values())
        # Измеряем время текущей попытки, включая ожидания.
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        # Считаем отзывы с текстом, датой, целой оценкой 1–5 и без сокращений.
        result["valid_reviews"] = sum(
            # Учитываем обязательные поля и отсутствие сокращения текста.
            bool(r["text"])
            # Учитываем обязательные поля и отсутствие сокращения текста.
            and bool(r.get("date"))
            # Учитываем обязательные поля и отсутствие сокращения текста.
            and type(r["rating"]) is int
            # Учитываем обязательные поля и отсутствие сокращения текста.
            and 1 <= r["rating"] <= 5
            # Учитываем обязательные поля и отсутствие сокращения текста.
            and not r["text_truncated"]
            # Последовательно обрабатываем записи указанного набора.
            for r in rows.values()
        )
        # Считаем отзывы без распознанной оценки.
        result["invalid_rating_count"] = sum(r["rating"] is None for r in rows.values())
        # Считаем тексты, которые остались сокращёнными.
        result["truncated_count"] = sum(r["text_truncated"] for r in rows.values())
        # Записываем число отзывов, найденных в этой попытке.
        result["observed_this_run"] = len(observed)
        # Записываем число отзывов, подтверждённых текущим DOM.
        result["observed_in_dom"] = len(dom_observed)
        # Сохраняем результат и журнал попыток на диск.
        save_result(target, result)

    # Обновляем отзыв по ключу, сохраняя уже полученный полный текст.
    def merge(row):
        # Дополняем отзыв идентификатором для объединения записей.
        row["id"] = review_key(oid, row)
        # Отмечаем отзыв как найденный в этой попытке.
        observed.add(row["id"])
        # Ищем ранее сохранённую версию этого отзыва.
        old = rows.get(row["id"])
        # Сохраняем полезные поля ранее полученной версии отзыва.
        if old and not old["text_truncated"] and row["text_truncated"]:
            # Завершаем этот этап без дополнительных действий.
            return
        # Сохраняем полезные поля ранее полученной версии отзыва.
        if old and old.get("source_review_id"):
            # Дополняем отзыв идентификатором для объединения записей.
            row["source_review_id"] = old["source_review_id"]
        # Обновляем запись отзыва по устойчивому ключу.
        rows[row["id"]] = row

    # Подключаем обработчик загрузки ресурсов страницы.
    page.route("**/*", route_request)
    # Подключаем обработчик кодов HTTP-ответов.
    page.on("response", observe_response)
    # Обрабатываем возможную ошибку следующего этапа.
    try:
        # Ограничиваем открытие страницы тремя попытками.
        for attempt in range(3):
            # Обрабатываем возможную ошибку следующего этапа.
            try:
                # Получаем ответ открытия страницы или его автономную подмену.
                response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                # Проверяем код ответа перед продолжением загрузки.
                if response and response.status in {502, 503, 504}:
                    # Не продолжаем открытие страницы после последней попытки.
                    if attempt == 2:
                        # Записываем причину завершения текущего этапа.
                        result["status"] = "server_error"
                        # Возвращаем результат; блок finally перед этим сохранит прогресс.
                        return result
                    # Выдерживаем паузу перед следующим обращением к сайту.
                    time.sleep(30 * (2**attempt))
                    # Переходим к следующей попытке или записи.
                    continue
                # Заканчиваем текущий цикл после полученного результата.
                break
            # Перехватываем указанную ошибку и сохраняем понятный итог.
            except BrowserTimeout:
                # Останавливаем обработку, если сайт требует проверку пользователя.
                if challenge(page) or stopped:
                    # Записываем причину завершения текущего этапа.
                    result["status"] = (
                        # Различаем проверку пользователя и техническую ошибку загрузки.
                        "captcha"
                        # Выбираем статус CAPTCHA при обнаружении проверки пользователя.
                        if challenge(page)
                        # Иначе используем обнаруженную причину ограничения доступа.
                        else stopped["status"]
                    )
                    # Возвращаем результат; блок finally перед этим сохранит прогресс.
                    return result
                # Не продолжаем открытие страницы после последней попытки.
                if attempt == 2:
                    # Записываем причину завершения текущего этапа.
                    result["status"] = "timeout"
                    # Возвращаем результат; блок finally перед этим сохранит прогресс.
                    return result
                # Выдерживаем паузу перед следующим обращением к сайту.
                time.sleep(30 * (2**attempt))

        # Сохраняем адрес после возможного перенаправления.
        result["final_url"] = page.url
        # Проверяем, зарегистрировано ли ограничение доступа.
        if stopped:
            # Переносим обнаруженную причину остановки в результат.
            result.update(stopped)
            # Возвращаем результат; блок finally перед этим сохранит прогресс.
            return result
        # Останавливаем обработку, если сайт требует проверку пользователя.
        if challenge(page):
            # Записываем причину завершения текущего этапа.
            result["status"] = "captcha"
            # Возвращаем результат; блок finally перед этим сохранит прогресс.
            return result
        # Извлекаем встроенное состояние страницы; при его отсутствии используем DOM.
        embedded = None
        # Обрабатываем возможную ошибку следующего этапа.
        try:
            # Извлекаем встроенное состояние страницы; при его отсутствии используем DOM.
            embedded = extract_saved_html(page.content(), oid)
            # Дополняем сведения об организации доступными значениями.
            result["company"].update(embedded["company"])
            # Последовательно обрабатываем записи указанного набора.
            for item in embedded["reviews"][:limit]:
                # Объединяем найденный отзыв с сохранёнными записями.
                merge(
                    {
                        # Сохраняем отображаемое имя автора.
                        "author": item["author"],
                        # Сохраняем публичную ссылку на профиль автора.
                        "author_url": f"https://yandex.ru/maps/user/{item['author_public_id']}"
                        # Строим ссылку на профиль только при наличии публичного идентификатора.
                        if item["author_public_id"]
                        # Указываем профиль автора только при наличии его публичного идентификатора.
                        else None,
                        # Сохраняем дату отзыва.
                        "date": item["date"],
                        # Сохраняем текст отзыва.
                        "text": item["text"],
                        # Сохраняем оценку отдельного отзыва.
                        "rating": item["rating"],
                        # Сохраняем признак сокращённого текста.
                        "text_truncated": False,
                        # Сохраняем исходный идентификатор отзыва из HTML.
                        "source_review_id": item["id"],
                    }
                )
            # Считаем тексты, полученные из встроенного HTML в пределах лимита.
            html_count = min(embedded["loaded_reviews"], limit)
            # Записываем число текстов, извлечённых из HTML.
            result["html_full_texts"] = html_count
        # Перехватываем указанную ошибку и сохраняем понятный итог.
        except (ValueError, KeyError, StopIteration):
            # Если формат встроенного JSON изменился, продолжаем чтение DOM.
            # Продолжаем чтение через DOM, если встроенный JSON недоступен.
            pass
        # Проверяем отсутствие отзывов по состоянию только что открытой страницы.
        if embedded is not None and embedded["company"].get("reviews_count") == 0:
            # Храним отзывы по уникальному ключу; при завершении оставляем подтверждённые в текущем DOM.
            rows = {}
            # Записываем причину завершения текущего этапа.
            result["status"] = "complete"
            # Возвращаем результат; блок finally перед этим сохранит прогресс.
            return result
        # Ждём появления карточек отзывов.
        page.wait_for_selector(CARDS, timeout=20000)
        # Считаем подряд идущие прокрутки без появления новых отзывов.
        stalls = 0
        # Ограничиваем число проходов чтения и прокрутки.
        for _ in range(25):
            # Останавливаем обработку, если сайт требует проверку пользователя.
            if challenge(page):
                # Записываем причину завершения текущего этапа.
                result["status"] = "captcha"
                # Заканчиваем текущий цикл после полученного результата.
                break
            # Сначала читаем DOM: найденные записи сохранятся и при последующем 429.
            # Читаем отзывы и счётчики из текущего DOM страницы.
            batch = page.evaluate(EXTRACT, limit)
            # Дополняем сведения об организации доступными значениями.
            result["company"].update(
                # Дополняем сведения только значениями, действительно найденными в DOM.
                {k: v for k, v in batch["company"].items() if v is not None}
            )
            # Последовательно обрабатываем записи указанного набора.
            for row in batch["reviews"]:
                # Объединяем найденный отзыв с сохранёнными записями.
                merge(row)
                # Отмечаем отзыв как присутствующий в текущем DOM.
                dom_observed.add(row["id"])
            # Сохраняем отзывы и показатели до следующего действия.
            checkpoint()
            # Проверяем, зарегистрировано ли ограничение доступа.
            if stopped:
                # Переносим обнаруженную причину остановки в результат.
                result.update(stopped)
                # Заканчиваем текущий цикл после полученного результата.
                break
            # Передаём браузеру ключи уже полных текстов, чтобы не раскрывать их повторно.
            full_keys = [
                # Преобразуем данные в JSON с заданными параметрами записи.
                json.dumps(
                    # Сопоставляем отзыв по ссылке на автора и дате.
                    [r["author_url"], r["date"]],
                    # Сохраняем кириллицу без преобразования в последовательности Unicode.
                    ensure_ascii=False,
                    # Используем компактную запись JSON для сопоставления ключей с браузером.
                    separators=(",", ":"),
                )
                # Последовательно обрабатываем записи указанного набора.
                for r in rows.values()
                # Передаём ключи только тех отзывов, чьи тексты уже получены полностью.
                if r["text"] and not r["text_truncated"]
            ]
            # Раскрываем только те сокращённые тексты, которых ещё нет полностью.
            expanded = page.evaluate(
                # Раскрываем недостающие тексты непосредственно в текущих DOM-карточках.
                r"""({limit, fullKeys}) => {
              // Считаем раскрытые тексты в этом проходе.
              let n=0;
              // Создаём множество ключей текстов, уже сохранённых полностью.
              const known = new Set(fullKeys);
              // Проверяем только карточки в пределах лимита.
              for (const card of [...document.querySelectorAll('.business-reviews-card-view__review')].slice(0,limit)) {
                // Сопоставляем автора и дату с уже сохранёнными текстами.
                const identity=JSON.stringify([
                  // Получаем ссылку на автора для сопоставления.
                  card.querySelector('.business-review-view__link')?.getAttribute('href') || null,
                  // Получаем дату отзыва для сопоставления.
                  card.querySelector('[itemprop="datePublished"]')?.getAttribute('content') || null,
                ]);
                // Пропускаем отзыв, полный текст которого уже сохранён.
                if (known.has(identity)) continue;
                // Ищем доступную кнопку раскрытия отзыва.
                const button=card.querySelector('.business-review-view__expand:not([aria-hidden="true"])');
                // Раскрываем свёрнутый текст и увеличиваем счётчик действий.
                if (button && card.querySelector('.spoiler-view__text._collapsed')) {button.click(); n++;}
              }
              // Возвращаем число раскрытых текстов.
              return n;
            }""",
                # Передаём лимит и ключи полных текстов в браузер.
                {"limit": limit, "fullKeys": full_keys},
            )
            # Учитываем раскрытые тексты.
            result["expanded_texts"] += expanded
            # Повторно читаем DOM после раскрытия текстов.
            if expanded:
                # Даём странице время завершить загрузку или раскрытие текста.
                page.wait_for_timeout(1000)
                # Читаем отзывы и счётчики из текущего DOM страницы.
                batch = page.evaluate(EXTRACT, limit)
                # Последовательно обрабатываем записи указанного набора.
                for row in batch["reviews"]:
                    # Объединяем найденный отзыв с сохранёнными записями.
                    merge(row)
                    # Отмечаем отзыв как присутствующий в текущем DOM.
                    dom_observed.add(row["id"])
                # Сохраняем отзывы и показатели до следующего действия.
                checkpoint()
            # Читаем число отзывов, указанное на карточке организации.
            declared = result["company"].get("reviews_count")
            # Сопоставляем найденные отзывы со счётчиком сайта или заданным лимитом.
            if declared is not None and len(dom_observed) >= declared:
                # Удаляем из завершённого снимка записи, которых уже нет в текущем DOM.
                # Храним отзывы по уникальному ключу; при завершении оставляем подтверждённые в текущем DOM.
                rows = {key: row for key, row in rows.items() if key in dom_observed}
                # Сохраняем отзывы и показатели до следующего действия.
                checkpoint()
                # Сопоставляем найденные отзывы со счётчиком сайта или заданным лимитом.
                if len(dom_observed) > declared:
                    # Записываем причину завершения текущего этапа.
                    result["status"] = "count_mismatch"
                # Обрабатываем альтернативный исход предыдущей проверки.
                else:
                    # Записываем причину завершения текущего этапа.
                    result["status"] = (
                        # Определяем итоговый статус с учётом полноты обязательных полей.
                        "complete"
                        # Проверяем структуру и уникальность сохранённых отзывов.
                        if result["valid_reviews"] == declared
                        # Определяем итоговый статус с учётом полноты обязательных полей.
                        else "incomplete_fields"
                    )
                # Заканчиваем текущий цикл после полученного результата.
                break
            # Сопоставляем найденные отзывы со счётчиком сайта или заданным лимитом.
            if len(dom_observed) >= limit:
                # Храним отзывы по уникальному ключу; при завершении оставляем подтверждённые в текущем DOM.
                rows = {key: row for key, row in rows.items() if key in dom_observed}
                # Сохраняем отзывы и показатели до следующего действия.
                checkpoint()
                # Записываем причину завершения текущего этапа.
                result["status"] = (
                    # Определяем итоговый статус с учётом полноты обязательных полей.
                    "capped"
                    # Проверяем структуру и уникальность сохранённых отзывов.
                    if result["valid_reviews"] >= limit
                    # Определяем итоговый статус с учётом полноты обязательных полей.
                    else "incomplete_fields"
                )
                # Заканчиваем текущий цикл после полученного результата.
                break
            # Проверяем, зарегистрировано ли ограничение доступа.
            if stopped:
                # Переносим обнаруженную причину остановки в результат.
                result.update(stopped)
                # Заканчиваем текущий цикл после полученного результата.
                break
            # Запоминаем число карточек перед прокруткой.
            before = page.locator(CARDS).count()
            # Находим и прокручиваем карточку одним действием: DOM может обновиться.
            # Выполняем чтение или прокрутку непосредственно в текущем DOM.
            page.evaluate(
                # Находим последнюю карточку и прокручиваем её панель одним действием.
                r"""selector => {
              // Заново находим последнюю карточку, не сохраняя устаревающий DOM-указатель.
              const last = [...document.querySelectorAll(selector)].at(-1);
              // Если карточек нет, прокрутка не требуется.
              if (!last) return;
              // Подводим последнюю карточку к нижней границе окна.
              last.scrollIntoView({block:'end'});
              // Поднимаемся по родительским элементам в поиске прокручиваемой панели.
              for (let parent=last.parentElement; parent; parent=parent.parentElement) {
                // Выбираем панель, содержимое которой действительно можно прокрутить.
                if (/auto|scroll/.test(getComputedStyle(parent).overflowY) && parent.scrollHeight > parent.clientHeight) {
                  // Прокручиваем найденную панель до конца.
                  parent.scrollTop=parent.scrollHeight;
                  // Заканчиваем поиск после прокрутки нужной панели.
                  break;
                }
              }
            }""",
                # Передаём селектор текущих карточек в браузер.
                CARDS,
            )
            # Учитываем выполненную прокрутку.
            result["scrolls"] += 1
            # Даём странице время завершить загрузку или раскрытие текста.
            page.wait_for_timeout(int(scroll_pause * 1000))
            # Обрабатываем возможную ошибку следующего этапа.
            try:
                # Ждём увеличения числа отзывов после прокрутки.
                page.wait_for_function(
                    # Задаём HTML-разметку или выражение проверки DOM.
                    f"document.querySelectorAll({json.dumps(CARDS)}).length > {before}",
                    # Ограничиваем ожидание в миллисекундах.
                    timeout=10000,
                )
                # Считаем подряд идущие прокрутки без появления новых отзывов.
                stalls = 0
            # Перехватываем указанную ошибку и сохраняем понятный итог.
            except BrowserTimeout:
                # Учитываем очередное ожидание без новых отзывов.
                stalls += 1
                # Завершаем попытку после трёх ожиданий без новых карточек.
                if stalls >= 3:
                    # Записываем причину завершения текущего этапа.
                    result["status"] = "stalled"
                    # Заканчиваем текущий цикл после полученного результата.
                    break
            # Останавливаем прокрутку после превышения бюджета текущей попытки.
            if time.monotonic() - started > 240:
                # Записываем причину завершения текущего этапа.
                result["status"] = "time_budget"
                # Заканчиваем текущий цикл после полученного результата.
                break
        # Обрабатываем альтернативный исход предыдущей проверки.
        else:
            # Записываем причину завершения текущего этапа.
            result["status"] = "scroll_budget"
    # Перехватываем указанную ошибку и сохраняем понятный итог.
    except BrowserTimeout as error:
        # Записываем причину завершения текущего этапа.
        result["status"] = (
            # Различаем проверку пользователя и техническую ошибку загрузки.
            "captcha"
            # Выбираем статус CAPTCHA при обнаружении проверки пользователя.
            if challenge(page)
            # При отсутствии ограничения доступа отмечаем таймаут ожидания.
            else (stopped.get("status") or "timeout")
        )
        # Сохраняем описание ошибки для диагностики.
        result["error"] = str(error).splitlines()[0]
    # Перехватываем указанную ошибку и сохраняем понятный итог.
    except Exception as error:
        # Записываем причину завершения текущего этапа.
        result["status"] = stopped.get("status") or "error"
        # Сохраняем описание ошибки для диагностики.
        result["error"] = f"{type(error).__name__}: {error}"
    # Выполняем сохранение или освобождение ресурсов при любом исходе.
    finally:
        # Проверяем, зарегистрировано ли ограничение доступа.
        if stopped:
            # Переносим обнаруженную причину остановки в результат.
            result.update(stopped)
        # Сохраняем отзывы и показатели до следующего действия.
        checkpoint()
        # Удаляем обработчик ресурсов перед следующей организацией.
        page.unroute("**/*", route_request)
        # Удаляем обработчик ответов перед следующей организацией.
        page.remove_listener("response", observe_response)
    # Возвращаем результат; блок finally перед этим сохранит прогресс.
    return result


# Разбираем параметры запуска и последовательно обрабатываем организации.
def main():
    # Создаём разборщик HTML или параметров командной строки.
    parser = argparse.ArgumentParser(description=__doc__)
    # Добавляем параметр командной строки с описанием.
    parser.add_argument(
        # Принимаем одну или несколько ссылок либо числовых идентификаторов.
        "urls",
        # Разрешаем передать одну или несколько организаций.
        nargs="+",
        # Показываем назначение параметра в справке запуска.
        help="Ссылки на карточки организаций или их числовые идентификаторы",
    )
    # Добавляем параметр командной строки с описанием.
    parser.add_argument(
        # Задаём имя параметра командной строки.
        "--limit",
        # Преобразуем ввод в требуемый тип данных.
        type=int,
        # Задаём значение параметра по умолчанию.
        default=100,
        # Показываем назначение параметра в справке запуска.
        help="Максимум отзывов на организацию: 1–300; по умолчанию 100",
    )
    # Добавляем параметр командной строки с описанием.
    parser.add_argument(
        # Задаём имя параметра командной строки.
        "--output",
        # Преобразуем ввод в требуемый тип данных.
        type=Path,
        # Задаём значение параметра по умолчанию.
        default=Path("review_results"),
        # Показываем назначение параметра в справке запуска.
        help="Папка с результатами и сохранённым прогрессом",
    )
    # Добавляем параметр командной строки с описанием.
    parser.add_argument(
        # Задаём имя параметра командной строки.
        "--pause",
        # Преобразуем ввод в требуемый тип данных.
        type=float,
        # Задаём значение параметра по умолчанию.
        default=20,
        # Показываем назначение параметра в справке запуска.
        help="Пауза между карточками в секундах: не менее 10",
    )
    # Добавляем параметр командной строки с описанием.
    parser.add_argument(
        # Задаём имя параметра командной строки.
        "--scroll-pause",
        # Преобразуем ввод в требуемый тип данных.
        type=float,
        # Задаём значение параметра по умолчанию.
        default=5,
        # Показываем назначение параметра в справке запуска.
        help="Пауза после прокрутки в секундах: не менее 3",
    )
    # Добавляем параметр командной строки с описанием.
    parser.add_argument(
        # Задаём имя параметра командной строки.
        "--429-retries",
        # Задаём имя параметра в разобранных аргументах.
        dest="rate_retries",
        # Преобразуем ввод в требуемый тип данных.
        type=int,
        # Задаём значение параметра по умолчанию.
        default=3,
        # Показываем назначение параметра в справке запуска.
        help="Число повторов после HTTP 429: 0–3",
    )
    # Добавляем параметр командной строки с описанием.
    parser.add_argument(
        # Задаём имя параметра командной строки.
        "--executable",
        # Описываем необязательный путь к установленному браузеру.
        help="Путь к исполняемому файлу установленного Chromium",
    )
    # Добавляем параметр командной строки с описанием.
    parser.add_argument(
        # Задаём имя параметра командной строки.
        "--refresh",
        # Обрабатываем параметр как переключатель.
        action="store_true",
        # Показываем назначение параметра в справке запуска.
        help="Повторно посетить карточки с уже завершённым результатом",
    )
    # Получаем значения параметров командной строки.
    args = parser.parse_args()
    # Проверяем связанные условия перед продолжением обработки.
    if (
        # Проверяем разрешённый диапазон параметра запуска.
        not 1 <= args.limit <= 300
        # Проверяем разрешённый диапазон параметра запуска.
        or args.pause < 10
        # Проверяем разрешённый диапазон параметра запуска.
        or args.scroll_pause < 3
        # Проверяем разрешённый диапазон параметра запуска.
        or not 0 <= args.rate_retries <= 3
    ):
        # Завершаем запуск с понятным описанием недопустимого параметра.
        parser.error(
            # Показываем допустимые диапазоны всех проверяемых параметров.
            "Требуется: limit 1–300; pause >= 10; scroll-pause >= 3; 429-retries 0–3"
        )
    # Обрабатываем возможную ошибку следующего этапа.
    try:
        # Проверяем адреса и исключаем повторяющиеся страницы отзывов.
        urls = list(dict.fromkeys(canonical_url(url) for url in args.urls))
    # Перехватываем указанную ошибку и сохраняем понятный итог.
    except ValueError as error:
        # Завершаем запуск с понятным описанием недопустимого параметра.
        parser.error(str(error))
    # Создаём папку результатов, если она ещё отсутствует.
    args.output.mkdir(parents=True, exist_ok=True)
    # Формируем очередь только из организаций, требующих обращения к сайту.
    pending = []
    # Проверяем сохранённый результат каждой организации до запуска браузера.
    for oid, url in urls:
        # Выбираем JSON-файл результатов одной организации.
        target = args.output / f"{oid}.json"
        # Читаем готовый результат для проверки возможности повторного использования.
        cached = (
            # Читаем сохранённый JSON; отсутствие файла означает пустой кэш.
            json.loads(target.read_text(encoding="utf-8"))
            # Читаем файл только при его наличии.
            if target.exists()
            # При отсутствии файла используем пустое сохранённое состояние.
            else {}
        )
        # Используем готовый кэш, если обновление не запрошено.
        if not args.refresh and cache_complete(cached, oid, args.limit):
            # Выводим краткое событие или результат в терминал.
            print(
                # Преобразуем данные в JSON с заданными параметрами записи.
                json.dumps(
                    {
                        # Сохраняем идентификатор организации.
                        "organization_id": oid,
                        # Сохраняем состояние обработки.
                        "status": "cache_hit",
                        # Сохраняем число отзывов с полными обязательными полями.
                        "valid_reviews": len(cached["reviews"]),
                    },
                    # Сохраняем кириллицу без преобразования в последовательности Unicode.
                    ensure_ascii=False,
                ),
                # Сразу показываем событие в терминале.
                flush=True,
            )
        # Обрабатываем альтернативный исход предыдущей проверки.
        else:
            # Добавляем организацию в очередь обращений к сайту.
            pending.append((oid, url, target))
    # Если очередь пуста, браузер запускать не требуется.
    if not pending:
        # Завершаем этот этап без дополнительных действий.
        return
    # Открываем Playwright с автоматическим освобождением ресурсов.
    with sync_playwright() as playwright:
        # Задаём параметры запуска Chromium.
        options = {"headless": True}
        # При необходимости используем указанный Chromium.
        if args.executable:
            # Передаём указанный путь к исполняемому файлу Chromium.
            options["executable_path"] = args.executable
        # Запускаем Chromium для чтения страниц.
        browser = playwright.chromium.launch(**options)
        # Обрабатываем возможную ошибку следующего этапа.
        try:
            # Открываем временную вкладку для определения версии браузера.
            probe = browser.new_page()
            # Читаем исходную строку User-Agent установленного Chromium.
            native_agent = probe.evaluate("navigator.userAgent")
            # Закрываем временную вкладку после чтения User-Agent.
            probe.close()
            # Используем обозначение Chrome с фактической версией браузера.
            user_agent = native_agent.replace("HeadlessChrome/", "Chrome/")
            # Создаём общую браузерную сессию с русской локалью и одним набором cookies.
            context = browser.new_context(
                # Используем русскую локаль страницы.
                locale="ru-RU",
                # Задаём размер окна для стабильной прокрутки панели.
                viewport={"width": 1366, "height": 900},
                # Используем обозначение Chrome с фактической версией браузера.
                user_agent=user_agent,
            )
            # Подготавливаем страницу для чтения DOM или автономную подмену браузера.
            page = context.new_page()
            # Обрабатываем карточки последовательно в одном браузере.
            for index, (oid, url, target) in enumerate(pending):
                # Выдерживаем межкарточную паузу начиная со второй организации.
                if index:
                    # Выдерживаем паузу перед следующим обращением к сайту.
                    time.sleep(args.pause)
                # Выводим краткое событие или результат в терминал.
                print(
                    # Преобразуем данные в JSON с заданными параметрами записи.
                    json.dumps(
                        # Сохраняем кириллицу без преобразования в последовательности Unicode.
                        {"organization_id": oid, "event": "open"},
                        # Сохраняем кириллицу в JSON без Unicode-последовательностей.
                        ensure_ascii=False,
                    ),
                    # Сразу показываем событие в терминале.
                    flush=True,
                )
                # Накапливаем сведения о попытках обработки текущей организации.
                attempts = []
                # Выполняем исходный сбор и разрешённое число повторов после 429.
                for retry in range(args.rate_retries + 1):
                    # Подготавливаем результат обработки и показатели полноты.
                    result = collect(
                        # Передаём странице организацию, путь результата и параметры сбора.
                        page,
                        # Передаём идентификатор текущей организации.
                        oid,
                        # Передаём канонический адрес страницы отзывов.
                        url,
                        # Передаём путь сохранения результатов текущей организации.
                        target,
                        # Передаём заданное число собираемых отзывов.
                        args.limit,
                        # Передаём длительность паузы после прокрутки.
                        args.scroll_pause,
                    )
                    # Сохраняем фактический User-Agent этой попытки.
                    result["user_agent"] = user_agent
                    # Добавляем сведения о текущей попытке в журнал.
                    attempts.append(
                        {
                            # Сохраняем номер попытки.
                            "attempt": retry + 1,
                            # Сохраняем время попытки в UTC.
                            "time_utc": result["collected_at"],
                            # Сохраняем состояние обработки.
                            "status": result["status"],
                            # Сохраняем число отзывов с полными обязательными полями.
                            "valid_reviews": result["valid_reviews"],
                            # Сохраняем HTTP-ошибки этой попытки.
                            "http_issues": result["network"]["issues"],
                        }
                    )
                    # Сохраняем журнал повторных попыток.
                    result["attempts"] = attempts.copy()
                    # Сохраняем результат и журнал попыток на диск.
                    save_result(target, result)
                    # Повторяем только HTTP 429 и только в пределах разрешённого числа попыток.
                    if result["status"] != "rate_limited" or retry == args.rate_retries:
                        # Заканчиваем текущий цикл после полученного результата.
                        break
                    # Выбираем большую из установленной задержки и серверного Retry-After.
                    pause = max(
                        # Увеличиваем паузу после 429: 60, 120 и 240 секунд.
                        60 * 2**retry,
                        # Учитываем длительность ожидания, указанную сервером.
                        retry_seconds(result.get("retry_after")),
                    )
                    # Не повторяем запрос раньше большого серверного Retry-After; оставляем сохранённый прогресс.
                    if pause > 900:
                        # Заканчиваем текущий цикл после полученного результата.
                        break
                    # Выводим краткое событие или результат в терминал.
                    print(
                        # Преобразуем данные в JSON с заданными параметрами записи.
                        json.dumps(
                            {
                                # Сохраняем событие для журнала в терминале.
                                "event": "cooldown",
                                # Сохраняем идентификатор организации.
                                "organization_id": oid,
                                # Сохраняем длительность ожидания в секундах.
                                "seconds": pause,
                                # Сохраняем номер повтора.
                                "retry": retry + 1,
                            }
                        ),
                        # Сразу показываем событие в терминале.
                        flush=True,
                    )
                    # На время ожидания сохраняем ту же сессию браузера и cookies.
                    # Выдерживаем паузу перед следующим обращением к сайту.
                    time.sleep(pause)
                # Выводим краткое событие или результат в терминал.
                print(
                    # Преобразуем данные в JSON с заданными параметрами записи.
                    json.dumps(
                        {
                            # Выбираем показатели для краткого журнала в терминале.
                            k: result[k]
                            # Последовательно обрабатываем записи указанного набора.
                            for k in [
                                # Указываем идентификатор организации.
                                "organization_id",
                                # Указываем состояние обработки.
                                "status",
                                # Указываем число отзывов с полными обязательными полями.
                                "valid_reviews",
                                # Выводим число сокращённых текстов.
                                "truncated_count",
                                # Выводим длительность последней попытки в секундах.
                                "elapsed_seconds",
                                # Указываем число прокруток.
                                "scrolls",
                                # Указываем число раскрытых текстов.
                                "expanded_texts",
                            ]
                        },
                        # Сохраняем кириллицу без преобразования в последовательности Unicode.
                        ensure_ascii=False,
                    ),
                    # Сразу показываем событие в терминале.
                    flush=True,
                )
                # Останавливаем всю очередь при сохраняющемся ограничении доступа.
                if result["status"] in {"captcha", "rate_limited", "access_denied"}:
                    # Заканчиваем текущий цикл после полученного результата.
                    break
        # Выполняем сохранение или освобождение ресурсов при любом исходе.
        finally:
            # Закрываем браузер даже при ошибке обработки.
            browser.close()


# Выполняем запуск только при прямом вызове файла.
if __name__ == "__main__":
    # Запускаем обработку параметров и очередь организаций.
    main()
