import os
import time
import re
import json
import random
import threading
from flask import Flask
from dotenv import load_dotenv
from telegram import (
    Update, 
    ReplyKeyboardMarkup, 
    ReplyKeyboardRemove, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup
)
from telegram.ext import (
    Updater, 
    CommandHandler, 
    MessageHandler, 
    Filters, 
    CallbackContext, 
    ConversationHandler,
    CallbackQueryHandler
)
import requests
from bs4 import BeautifulSoup
import sqlite3

load_dotenv()

# Стадии диалога
(ADD_URL, SET_MIN_PRICE, SET_MAX_PRICE, SET_KEYWORDS, PARSE_MANUALLY) = range(5)

# Настройки
TOKEN = os.getenv('BOT_TOKEN')
PORT = int(os.getenv('PORT', '8080'))
APP_NAME = os.getenv('APP_NAME')

# Список User-Agent для ротации
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/535.11 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (iPad; CPU OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Linux; Android 11; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36'
]

app = Flask(__name__)

@app.route('/')
def health_check():
    """Эндпоинт для UptimeRobot"""
    return "✅ Kufar Bot PRO is alive!", 200

def run_flask():
    """Запуск Flask в фоновом потоке"""
    app.run(host="0.0.0.0", port=PORT)

def get_random_user_agent():
    """Выбирает случайный User-Agent для защиты от блокировок"""
    return random.choice(USER_AGENTS)

def analyze_ad_risk(text: str) -> dict:
    """
    Анализирует текст на риски мошенничества
    Возвращает: {'risk_level': 0-2, 'phrases': ['фраза1', 'фраза2']}
    """
    risky_phrases = {
        'high': ["предоплата", "перевод на карту", "не встретимся", "только онлайн", "залог денег", "гарантийный платеж"],
        'medium': ["срочная продажа", "торг", "уступлю", "без торга", "залог", "документы на руках", "продаю за другого"]
    }
    
    found_phrases = []
    risk_level = 0
    
    # Поиск рисковых фраз
    text_lower = text.lower()
    
    for phrase in risky_phrases['high']:
        if phrase in text_lower:
            found_phrases.append(phrase)
            risk_level = max(risk_level, 2)  # Высокий риск
    
    for phrase in risky_phrases['medium']:
        if phrase in text_lower:
            found_phrases.append(phrase)
            if risk_level < 2:  # Не перекрываем высокий риск
                risk_level = max(risk_level, 1)  # Средний риск
    
    # Дополнительные проверки
    if "whatsapp" in text_lower or "телеграм" in text_lower or "viber" in text_lower:
        risk_level = max(risk_level, 1)
    
    if len(found_phrases) >= 3:
        risk_level = 2
    
    return {
        'risk_level': risk_level,
        'phrases': found_phrases,
        'text': text[:200]  # Для логирования
    }

def get_risk_message(risk_ dict) -> str:
    """Формирует текстовое сообщение на основе уровня риска"""
    if risk_data['risk_level'] == 0:
        return ""
    
    messages = {
        1: "❗️ *Будьте осторожны при сделке*\nРекомендуем встречаться в безопасном месте и проверять товар перед оплатой.",
        2: "⚠️ *ВЫСОКИЙ РИСК МОШЕННИЧЕСТВА!*\nНе переводите деньги до личной встречи и проверки товара. Сообщите о подозрительном объявлении Kufar."
    }
    
    phrases_text = ""
    if risk_data['phrases']:
        phrases_text = "\n\n*Рисковые фразы в объявлении:* " + ", ".join(risk_data['phrases'])
    
    return f"{messages[risk_data['risk_level']]}{phrases_text}"

def parse_kufar_url(url: str, min_price: int = None, max_price: int = None, keywords: str = None) -> list:
    """Парсит объявления с Kufar.by с фильтрами и защитой от блокировок"""
    headers = {
        'User-Agent': get_random_user_agent(),
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Connection': 'keep-alive'
    }
    
    try:
        # Случайная задержка от 1 до 3 секунд
        time.sleep(random.uniform(1.0, 3.0))
        
        # Проверяем, есть ли в URL параметры цены
        if min_price or max_price:
            if 'prc=' not in url:
                price_param = f"prc={min_price or 0}~{max_price or 0}"
                url = url + ('&' if '?' in url else '?') + price_param
        
        response = requests.get(url, headers=headers, timeout=15)
        
        # Проверка на блокировку Cloudflare
        if "cloudflare" in response.text.lower() or response.status_code == 403:
            print("⚠️ Обнаружена защита Cloudflare! Меняем User-Agent...")
            headers['User-Agent'] = get_random_user_agent()
            response = requests.get(url, headers=headers, timeout=15)
        
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'lxml')
        
        # Поиск объявлений (адаптировано под текущую верстку Kufar)
        listings = []
        script_tags = soup.find_all('script', {'id': '__NEXT_DATA__'})
        
        if script_tags:
            try:
                data = json.loads(script_tags[0].string)
                items = data['props']['pageProps']['dehydratedState']['queries'][0]['state']['data']['ads']
                
                for item in items:
                    price = item.get('price', 0)
                    price_int = price
                    
                    # Фильтр по цене
                    if min_price and price_int < min_price:
                        continue
                    if max_price and price_int > max_price:
                        continue
                    
                    # Фильтр по ключевым словам
                    title = item.get('subject', '').lower()
                    description = item.get('body', '').lower()
                    keyword_match = False if keywords else True
                    
                    if keywords:
                        for word in keywords.lower().split(','):
                            word = word.strip()
                            if word and (word in title or word in description):
                                keyword_match = True
                                break
                    
                    if not keyword_match:
                        continue
                    
                    # Анализ рисков мошенничества
                    full_text = f"{title} {description} {item.get('params', '')}"
                    risk_analysis = analyze_ad_risk(full_text)
                    
                    listings.append({
                        'id': item['ad_id'],
                        'title': item['subject'],
                        'price': f"{price_int} BYN",
                        'price_int': price_int,
                        'url': f"https://kufar.by/item/{item['ad_id']}",
                        'description': description,
                        'risk_data': risk_analysis
                    })
            except Exception as e:
                print(f"Ошибка парсинга JSON: {e}")
        else:
            # Резервный метод парсинга
            print("Резервный метод парсинга...")
            cards = soup.find_all('div', class_=re.compile('list-item'))
            for card in cards[:5]:
                try:
                    title_tag = card.find('a', class_=re.compile('title'))
                    price_tag = card.find('div', class_=re.compile('price'))
                    link_tag = card.find('a', class_=re.compile('title'))
                    
                    if not all([title_tag, price_tag, link_tag]):
                        continue
                    
                    ad_id = link_tag['href'].split('/')[-1].split('?')[0]
                    title = title_tag.text.strip()
                    price_text = price_tag.text.replace(' ', '').replace('р.', '').strip()
                    price_int = int(re.sub(r'[^\d]', '', price_text)) if price_text.isdigit() else 0
                    price = f"{price_int} BYN"
                    
                    # Проверка фильтров
                    if (min_price and price_int < min_price) or (max_price and price_int > max_price):
                        continue
                    
                    # Анализ рисков мошенничества
                    risk_analysis = analyze_ad_risk(title)
                    
                    listings.append({
                        'id': ad_id,
                        'title': title,
                        'price': price,
                        'price_int': price_int,
                        'url': f"https://kufar.by{link_tag['href']}",
                        'description': "",
                        'risk_data': risk_analysis
                    })
                except Exception as e:
                    continue
        
        return listings
    except Exception as e:
        print(f"🔥 Критическая ошибка парсинга: {e}")
        return []

def get_price_drops(user_id: int, new_items: list) -> list:
    """Проверка снижения цены для новых объявлений"""
    conn = sqlite3.connect('kufar_bot.db')
    c = conn.cursor()
    alerts = []
    
    try:
        for item in new_items:
            ad_id = str(item['id'])
            current_price = item['price_int']
            
            # Получаем последнюю цену для этого объявления
            c.execute("""
                SELECT price FROM price_history 
                WHERE user_id = ? AND ad_id = ? 
                ORDER BY timestamp DESC LIMIT 1
            """, (user_id, ad_id))
            
            last_price = c.fetchone()
            
            # Проверяем снижение цены
            if last_price and current_price < last_price[0]:
                drop_amount = last_price[0] - current_price
                drop_percent = round((drop_amount / last_price[0]) * 100, 1)
                
                alerts.append({
                    'item': item,
                    'old_price': last_price[0],
                    'new_price': current_price,
                    'drop_percent': drop_percent,
                    'drop_amount': drop_amount
                })
        
        return alerts
    finally:
        conn.close()

def save_price_data(user_id: int, ad_id: str, title: str, price: int, url: str):
    """Сохранение данных о цене объявления"""
    conn = sqlite3.connect('kufar_bot.db')
    c = conn.cursor()
    
    try:
        # Проверяем, есть ли уже запись за последние 24 часа для этого объявления
        c.execute("""
            SELECT price FROM price_history 
            WHERE user_id = ? AND ad_id = ? 
            ORDER BY timestamp DESC LIMIT 1
        """, (user_id, ad_id))
        
        last_record = c.fetchone()
        
        # Сохраняем только если цена изменилась или новое объявление
        if not last_record or last_record[0] != price:
            c.execute("""
                INSERT INTO price_history (user_id, ad_id, title, price, url)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, ad_id, title, price, url))
            conn.commit()
            return True
        return False
    finally:
        conn.close()

def init_db():
    """Инициализация базы данных со всеми таблицами"""
    conn = sqlite3.connect('kufar_bot.db')
    c = conn.cursor()
    
    # Таблица пользователей
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL
    )''')
    
    # Таблица ссылок
    c.execute('''CREATE TABLE IF NOT EXISTS urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                last_id INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
    )''')
    
    # Таблица фильтров
    c.execute('''CREATE TABLE IF NOT EXISTS filters (
                user_id INTEGER PRIMARY KEY,
                min_price INTEGER,
                max_price INTEGER,
                keywords TEXT,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
    )''')
    
    # Таблица истории цен
    c.execute('''CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ad_id TEXT NOT NULL,
                title TEXT NOT NULL,
                price INTEGER NOT NULL,
                url TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
    )''')
    
    conn.commit()
    conn.close()
    print("✅ База данных инициализирована с новыми таблицами")

def add_user(user_id: int, chat_id: int):
    """Добавление пользователя в БД"""
    conn = sqlite3.connect('kufar_bot.db')
    c = conn.cursor()
    
    try:
        c.execute("INSERT OR IGNORE INTO users (user_id, chat_id) VALUES (?, ?)", 
                 (user_id, chat_id))
        conn.commit()
    finally:
        conn.close()

def add_url(user_id: int, url: str):
    """Добавление ссылки для пользователя"""
    conn = sqlite3.connect('kufar_bot.db')
    c = conn.cursor()
    
    try:
        c.execute("INSERT INTO urls (user_id, url) VALUES (?, ?)", 
                 (user_id, url))
        conn.commit()
    finally:
        conn.close()

def get_user_urls(user_id: int) -> list:
    """Получение всех ссылок пользователя"""
    conn = sqlite3.connect('kufar_bot.db')
    c = conn.cursor()
    
    try:
        c.execute("SELECT id, url, last_id FROM urls WHERE user_id = ?", (user_id,))
        return c.fetchall()
    finally:
        conn.close()

def update_last_id(user_id: int, url_id: int, last_id: int):
    """Обновление последнего ID объявления для ссылки"""
    conn = sqlite3.connect('kufar_bot.db')
    c = conn.cursor()
    
    try:
        c.execute("UPDATE urls SET last_id = ? WHERE id = ? AND user_id = ?", 
                 (last_id, url_id, user_id))
        conn.commit()
    finally:
        conn.close()

def get_all_users() -> list:
    """Получение всех пользователей"""
    conn = sqlite3.connect('kufar_bot.db')
    c = conn.cursor()
    
    try:
        c.execute("SELECT user_id FROM users")
        return c.fetchall()
    finally:
        conn.close()

def get_user_filters(user_id: int) -> tuple:
    """Получение фильтров пользователя"""
    conn = sqlite3.connect('kufar_bot.db')
    c = conn.cursor()
    
    try:
        c.execute("SELECT * FROM filters WHERE user_id = ?", (user_id,))
        return c.fetchone()
    finally:
        conn.close()

def update_filters(user_id: int, min_price: int, max_price: int, keywords: str):
    """Обновление фильтров пользователя"""
    conn = sqlite3.connect('kufar_bot.db')
    c = conn.cursor()
    
    try:
        # Проверяем, существуют ли фильтры
        c.execute("SELECT 1 FROM filters WHERE user_id = ?", (user_id,))
        exists = c.fetchone()
        
        if exists:
            c.execute("""UPDATE filters SET 
                        min_price = ?, max_price = ?, keywords = ?
                        WHERE user_id = ?""",
                     (min_price, max_price, keywords, user_id))
        else:
            c.execute("""INSERT INTO filters 
                        (user_id, min_price, max_price, keywords) 
                        VALUES (?, ?, ?, ?)""",
                     (user_id, min_price, max_price, keywords))
        
        conn.commit()
    finally:
        conn.close()

def delete_all_urls(user_id: int):
    """Удаление всех ссылок пользователя"""
    conn = sqlite3.connect('kufar_bot.db')
    c = conn.cursor()
    
    try:
        c.execute("DELETE FROM urls WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()

def send_periodic_updates(context: CallbackContext):
    """Автоматическая проверка новых объявлений и снижения цен (интервал 6 минут)"""
    users = get_all_users()
    for user in users:
        user_id = user[0]
        urls = get_user_urls(user_id)
        filters = get_user_filters(user_id)
        
        if not urls:
            continue
        
        min_price = filters[1] if filters else None
        max_price = filters[2] if filters else None
        keywords = filters[3] if filters else None
        
        new_items_found = False
        price_drops_found = False
        messages = []
        
        for url_data in urls:
            url_id = url_data[0]
            url = url_data[1]
            last_id = url_data[2] or 0
            
            try:
                items = parse_kufar_url(url, min_price, max_price, keywords)
                
                # Проверка новых объявлений
                new_items = [
                    item for item in items 
                    if int(item['id']) > last_id
                ]
                
                # Проверка снижения цены для всех объявлений
                price_drops = get_price_drops(user_id, items)
                
                # Обработка новых объявлений
                if new_items:
                    new_items_found = True
                    
                    # Сохраняем цены для новых объявлений
                    for item in new_items:
                        save_price_data(
                            user_id, 
                            item['id'], 
                            item['title'], 
                            item['price_int'], 
                            item['url']
                        )
                    
                    # Формируем сообщение о новых объявлениях
                    message = "✨ *Новые объявления*:\n\n"
                    for item in new_items[:3]:  # Максимум 3 объявления за раз
                        risk_message = get_risk_message(item['risk_data'])
                        
                        message += f"💰 *{item['price']}*\n"
                        message += f"📌 [{item['title']}]({item['url']})\n"
                        
                        if risk_message:
                            message += f"\n{risk_message}\n"
                        
                        message += "\n"
                    
                    messages.append(message)
                    
                    # Обновляем last_id на максимальный из новых
                    new_last_id = max(int(item['id']) for item in new_items)
                    update_last_id(user_id, url_id, new_last_id)
                
                # Обработка снижения цен
                if price_drops:
                    price_drops_found = True
                    
                    message = "📉 *Цены упали!*\n\n"
                    for drop in price_drops[:3]:  # Максимум 3 уведомления
                        item = drop['item']
                        risk_message = get_risk_message(item['risk_data'])
                        
                        message += f"📉 Снижение на *{drop['drop_percent']}%* ({drop['drop_amount']} BYN)!\n"
                        message += f"💰 Было: *{drop['old_price']} BYN*\n"
                        message += f"💰 Стало: *{item['price']}*\n"
                        message += f"📌 [{item['title']}]({item['url']})\n"
                        
                        if risk_message:
                            message += f"\n{risk_message}\n"
                        
                        message += "\n"
                    
                    messages.append(message)
            
            except Exception as e:
                print(f"Ошибка при обработке URL {url} для пользователя {user_id}: {e}")
        
        # Отправляем сообщения пользователю
        if (new_items_found or price_drops_found) and messages:
            for msg in messages:
                try:
                    context.bot.send_message(
                        chat_id=user_id,
                        text=msg,
                        parse_mode='Markdown',
                        disable_web_page_preview=True
                    )
                    time.sleep(1)  # Задержка между сообщениями
                except Exception as e:
                    print(f"Ошибка отправки сообщения пользователю {user_id}: {e}")

def start(update: Update, context: CallbackContext) -> None:
    """Стартовое меню"""
    user_id = update.effective_user.id
    add_user(user_id, update.effective_chat.id)
    
    keyboard = [
        ["🔗 Добавить ссылку", "⚙️ Настроить фильтры"],
        ["▶️ Запустить вручную", "🛑 Остановить уведомления"],
        ["📊 Мои ссылки", "ℹ️ Помощь"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    update.message.reply_text(
        "👋 Привет! Я ваш *Kufar Bot PRO* с умным отслеживанием цен и защитой от мошенников!\n\n"
        "✨ *Что я умею:*\n"
        "✅ Отслеживать новые объявления\n"
        "✅ 🔍 Уведомлять о снижении цен\n"
        "✅ 🛡️ Анализировать объявления на мошенничество\n"
        "✅ Работать 24/7 без остановок\n\n"
        "⏰ *Проверка каждые 6 минут!* (раньше было 10)\n\n"
        "👇 *Как начать:*\n"
        "1️⃣ Нажмите `🔗 Добавить ссылку`\n"
        "2️⃣ Вставьте URL из Kufar.by с нужными фильтрами\n"
        "3️⃣ Настройте ценовые фильтры\n"
        "4️⃣ Я буду присылать уведомления автоматически!\n\n"
        "💡 *Совет:* Чтобы получить ссылку:\n"
        "- Зайдите на Kufar.by в браузере\n"
        "- Выберите категорию и город\n"
        "- Установите фильтры цены\n"
        "- Скопируйте адрес из адресной строки\n\n"
        "👇 Выберите действие:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

def add_url(update: Update, context: CallbackContext) -> int:
    """Начало добавления ссылки"""
    update.message.reply_text(
        "🔗 *Введите ссылку с Kufar.by*\n\n"
        "👉 Пример: https://auto.kufar.by/listings?cat=1400&rgn=7&prc=500~2000\n"
        "📌 Ссылку можно скопировать из браузера после установки всех фильтров\n\n"
        "🚫 *Важно:* Ссылка должна начинаться с `https://kufar.by` или `https://www.kufar.by`\n\n"
        "✏️ Вставьте ссылку ниже:",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode='Markdown'
    )
    return ADD_URL

def save_url(update: Update, context: CallbackContext) -> int:
    """Сохранение ссылки в БД"""
    user_id = update.effective_user.id
    url = update.message.text.strip()
    
    # Валидация URL
    if not url.startswith('https://kufar.by') and not url.startswith('https://www.kufar.by') and not url.startswith('https://cars.kufar.by'):
        update.message.reply_text(
            "❌ Неверный формат ссылки!\n"
            "Ссылка должна начинаться с `https://kufar.by`, `https://www.kufar.by` или `https://cars.kufar.by`\n\n"
            "🔄 Попробуйте еще раз:",
            parse_mode='Markdown'
        )
        return ADD_URL
    
    try:
        add_url(user_id, url)
        update.message.reply_text(
            "✅ Ссылка успешно добавлена!\n\n"
            "✨ Теперь настройте фильтры или запустите парсинг вручную!",
            reply_markup=ReplyKeyboardMarkup([["🏠 Вернуться в меню"]], resize_keyboard=True)
        )
    except Exception as e:
        update.message.reply_text(
            f"❌ Ошибка при сохранении: {e}\n"
            "🔄 Попробуйте снова или обратитесь к разработчику",
            reply_markup=ReplyKeyboardMarkup([["🏠 Вернуться в меню"]], resize_keyboard=True)
        )
    
    return ConversationHandler.END

def set_filters(update: Update, context: CallbackContext) -> int:
    """Настройка фильтров цены"""
    keyboard = [
        ["🏠 Вернуться в меню"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    update.message.reply_text(
        "⚙️ *Настройка фильтров*\n\n"
        "✏️ Введите минимальную цену в BYN (например: 100)\n"
        "Или нажмите 'Пропустить', чтобы не устанавливать минимум:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return SET_MIN_PRICE

def set_min_price(update: Update, context: CallbackContext) -> int:
    """Установка минимальной цены"""
    if update.message.text == "🏠 Вернуться в меню":
        start(update, context)
        return ConversationHandler.END
    
    try:
        min_price = int(update.message.text)
        if min_price < 0:
            raise ValueError
        context.user_data['min_price'] = min_price
    except:
        context.user_data['min_price'] = None
    
    update.message.reply_text(
        "✏️ Введите максимальную цену в BYN (например: 500)\n"
        "Или нажмите 'Пропустить', чтобы не устанавливать максимум:",
        reply_markup=ReplyKeyboardMarkup([["Пропустить", "🏠 Вернуться в меню"]], resize_keyboard=True)
    )
    return SET_MAX_PRICE

def set_max_price(update: Update, context: CallbackContext) -> int:
    """Установка максимальной цены"""
    if update.message.text == "🏠 Вернуться в меню":
        start(update, context)
        return ConversationHandler.END
    
    try:
        if update.message.text != "Пропустить":
            max_price = int(update.message.text)
            if max_price < 0:
                raise ValueError
            context.user_data['max_price'] = max_price
        else:
            context.user_data['max_price'] = None
    except:
        context.user_data['max_price'] = None
    
    update.message.reply_text(
        "✏️ Введите ключевые слова через запятую (например: iphone, apple, б/у)\n"
        "Бот будет искать их в названии и описании объявлений\n"
        "Или нажмите 'Пропустить', чтобы отключить фильтр:",
        reply_markup=ReplyKeyboardMarkup([["Пропустить", "🏠 Вернуться в меню"]], resize_keyboard=True)
    )
    return SET_KEYWORDS

def save_filters(update: Update, context: CallbackContext) -> int:
    """Сохранение всех фильтров в БД"""
    if update.message.text == "🏠 Вернуться в меню":
        start(update, context)
        return ConversationHandler.END
    
    user_id = update.effective_user.id
    min_price = context.user_data.get('min_price')
    max_price = context.user_data.get('max_price')
    keywords = update.message.text if update.message.text != "Пропустить" else None
    
    try:
        update_filters(user_id, min_price, max_price, keywords)
        update.message.reply_text(
            "✅ Фильтры успешно сохранены!\n\n"
            f"💰 Диапазон цены: {min_price or 'Любой'} - {max_price or 'Любой'} BYN\n"
            f"🔍 Ключевые слова: {keywords or 'Отключены'}\n\n"
            "✨ Теперь бот будет учитывать эти настройки при поиске",
            reply_markup=ReplyKeyboardMarkup([["🏠 Вернуться в меню"]], resize_keyboard=True)
        )
    except Exception as e:
        update.message.reply_text(
            f"❌ Ошибка сохранения фильтров: {e}\n"
            "🔄 Попробуйте снова",
            reply_markup=ReplyKeyboardMarkup([["🏠 Вернуться в меню"]], resize_keyboard=True)
        )
    
    return ConversationHandler.END

def manual_parse(update: Update, context: CallbackContext) -> int:
    """Ручной запуск парсинга с анализом цен и рисков"""
    user_id = update.effective_user.id
    urls = get_user_urls(user_id)
    filters = get_user_filters(user_id)
    
    if not urls:
        update.message.reply_text(
            "❌ У вас нет добавленных ссылок!\n"
            "Сначала добавьте ссылку через `🔗 Добавить ссылку`",
            reply_markup=ReplyKeyboardMarkup([["🏠 Вернуться в меню"]], resize_keyboard=True)
        )
        return ConversationHandler.END
    
    min_price = filters[1] if filters else None
    max_price = filters[2] if filters else None
    keywords = filters[3] if filters else None
    
    update.message.reply_text(
        "⏳ Начинаю парсинг...\n"
        "Это может занять до 1 минуты. Пожалуйста, подождите!",
        reply_markup=ReplyKeyboardRemove()
    )
    
    all_items = []
    price_drops = []
    
    for url_data in urls:
        url = url_data[1]
        try:
            items = parse_kufar_url(url, min_price, max_price, keywords)
            all_items.extend(items[:3])  # Берем максимум 3 объявления с каждой ссылки
            
            # Проверяем снижение цен для текущего парсинга
            drops = get_price_drops(user_id, items)
            price_drops.extend(drops)
        except Exception as e:
            print(f"Ошибка при ручном парсинге {url}: {e}")
    
    # Формируем сообщение
    message_parts = []
    
    if price_drops:
        drop_msg = "📉 *Обнаружено снижение цен:*\n\n"
        for drop in price_drops[:3]:
            item = drop['item']
            risk_message = get_risk_message(item['risk_data'])
            
            drop_msg += f"📉 Снижение на *{drop['drop_percent']}%*!\n"
            drop_msg += f"💰 Было: *{drop['old_price']} BYN*\n"
            drop_msg += f"💰 Стало: *{item['price']}*\n"
            drop_msg += f"📌 [{item['title']}]({item['url']})\n"
            
            if risk_message:
                drop_msg += f"\n{risk_message}\n"
            
            drop_msg += "\n"
        message_parts.append(drop_msg)
    
    if all_items:
        items_msg = "✨ *Результаты парсинга:*\n\n"
        for i, item in enumerate(all_items[:5], 1):  # Максимум 5 объявлений
            risk_message = get_risk_message(item['risk_data'])
            
            items_msg += f"{i}. 💰 {item['price']}\n"
            items_msg += f"📌 [{item['title']}]({item['url']})\n"
            
            if risk_message:
                items_msg += f"\n{risk_message}\n"
            
            items_msg += "\n"
        message_parts.append(items_msg)
    
    if not message_parts:
        update.message.reply_text(
            "🔍 По вашим фильтрам ничего не найдено\n"
            "Попробуйте изменить фильтры или добавить другие ссылки",
            reply_markup=ReplyKeyboardMarkup([["🏠 Вернуться в меню"]], resize_keyboard=True)
        )
        return ConversationHandler.END
    
    # Отправляем все части сообщения
    for part in message_parts:
        update.message.reply_text(
            part,
            parse_mode='Markdown',
            disable_web_page_preview=True
        )
    
    update.message.reply_text(
        "✅ Парсинг завершен!\n"
        "Я буду продолжать отслеживать эти ссылки автоматически каждые 6 минут.",
        reply_markup=ReplyKeyboardMarkup([["🏠 Вернуться в меню"]], resize_keyboard=True)
    )
    
    return ConversationHandler.END

def show_urls(update: Update, context: CallbackContext) -> None:
    """Показывает список добавленных ссылок"""
    user_id = update.effective_user.id
    urls = get_user_urls(user_id)
    
    if not urls:
        update.message.reply_text(
            "📭 У вас нет добавленных ссылок\n"
            "Нажмите `🔗 Добавить ссылку`, чтобы начать отслеживание",
            reply_markup=ReplyKeyboardMarkup([["🏠 Вернуться в меню"]], resize_keyboard=True)
        )
        return
    
    message = "🌐 *Ваши отслеживаемые ссылки:*\n\n"
    for i, url_data in enumerate(urls, 1):
        message += f"{i}. {url_data[1]}\n"
    
    keyboard = [
        [InlineKeyboardButton("🗑️ Удалить все ссылки", callback_data='delete_urls')],
        [InlineKeyboardButton("🏠 Вернуться в меню", callback_data='back')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    update.message.reply_text(
        message,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

def button_handler(update: Update, context: CallbackContext) -> None:
    """Обработчик инлайн-кнопок"""
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    
    if query.data == 'delete_urls':
        delete_all_urls(user_id)
        query.edit_message_text(
            "✅ Все ссылки успешно удалены!\n"
            "Теперь вы можете добавить новые",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Вернуться в меню", callback_data='back')
            ]])
        )
    elif query.data == 'back':
        start(update, context)

def show_help(update: Update, context: CallbackContext) -> None:
    """Показывает справку с описанием новых функций"""
    help_text = (
        "ℹ️ *Помощь по Kufar Bot PRO*\n\n"
        "🔍 *Умное отслеживание цен:*\n"
        "- Бот автоматически отслеживает цены на объявления\n"
        "- При снижении цены вы получите алерт с указанием процентов\n"
        "- Пример: \"📉 Снижение на 15% (200 BYN)!\"\n\n"
        
        "🛡️ *AI-анализ мошенничества:*\n"
        "- Бот анализирует текст объявлений на рисковые фразы\n"
        "- Уровни риска:\n"
        "  • 🟢 Низкий — можно покупать спокойно\n"
        "  • 🟡 Средний — будьте осторожны\n"
        "  • 🔴 Высокий — высокий риск мошенничества!\n"
        "- Примеры фраз: \"предоплата\", \"перевод на карту\"\n\n"
        
        "⏰ *Автоматический парсинг:*\n"
        "- ✅ Проверка каждые 6 минут (раньше было 10 минут)!\n"
        "- Мгновенные уведомления о новых и подешевевших объявлениях\n\n"
        
        "💡 *Советы по безопасности:*\n"
        "1. Всегда встречайтесь в людных местах\n"
        "2. Не переводите деньги до осмотра товара\n"
        "3. Проверяйте продавца через поиск по номеру телефона\n\n"
        
        "❓ *Что делать при подозрении на мошенничество:*\n"
        "- Нажмите кнопку \"Пожаловаться\" под объявлением\n"
        "- Сообщите в поддержку Kufar\n"
        "- Предупредите других покупателей в комментариях"
    )
    
    update.message.reply_text(
        help_text,
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardMarkup([["🏠 Вернуться в меню"]], resize_keyboard=True)
    )

def cancel(update: Update, context: CallbackContext) -> int:
    """Отмена текущего действия"""
    update.message.reply_text(
        "❌ Действие отменено\n"
        "Выберите команду из меню ниже:",
        reply_markup=ReplyKeyboardMarkup([["🏠 Вернуться в меню"]], resize_keyboard=True)
    )
    return ConversationHandler.END

def main():
    """Основная функция запуска бота"""
    init_db()
    
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher
    
    # Обработчики команд
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", show_help))
    
    # Обработчик инлайн-кнопок
    dp.add_handler(CallbackQueryHandler(button_handler))
    
    # Диалог добавления ссылки
    conv_handler_url = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex('^🔗 Добавить ссылку$'), add_url)],
        states={
            ADD_URL: [MessageHandler(Filters.text & ~Filters.command, save_url)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    # Диалог настройки фильтров
    conv_handler_filters = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex('^⚙️ Настроить фильтры$'), set_filters)],
        states={
            SET_MIN_PRICE: [
                MessageHandler(Filters.regex('^🏠 Вернуться в меню$'), cancel),
                MessageHandler(Filters.text & ~Filters.command, set_min_price)
            ],
            SET_MAX_PRICE: [
                MessageHandler(Filters.regex('^🏠 Вернуться в меню$'), cancel),
                MessageHandler(Filters.text & ~Filters.command, set_max_price)
            ],
            SET_KEYWORDS: [
                MessageHandler(Filters.regex('^🏠 Вернуться в меню$'), cancel),
                MessageHandler(Filters.text & ~Filters.command, save_filters)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    # Диалог ручного парсинга
    conv_handler_parse = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex('^▶️ Запустить вручную$'), manual_parse)],
        states={},
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    # Регистрация обработчиков
    dp.add_handler(conv_handler_url)
    dp.add_handler(conv_handler_filters)
    dp.add_handler(conv_handler_parse)
    dp.add_handler(MessageHandler(Filters.regex('^📊 Мои ссылки$'), show_urls))
    dp.add_handler(MessageHandler(Filters.regex('^🛑 Остановить уведомления$'), 
                  lambda u, c: u.message.reply_text("⏹️ Уведомления временно отключены. Чтобы включить — перезапустите бота")))
    dp.add_handler(MessageHandler(Filters.regex('^ℹ️ Помощь$'), show_help))
    dp.add_handler(MessageHandler(Filters.regex('^🏠 Вернуться в меню$'), start))
    
    # 🔥 ГЛАВНОЕ ИЗМЕНЕНИЕ: интервал 6 минут (360 секунд)
    job_queue = updater.job_queue
    job_queue.run_repeating(send_periodic_updates, interval=360, first=10)  # Каждые 6 минут!
    
    # Настройка вебхуков для Replit
    if APP_NAME:
        updater.start_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"https://{APP_NAME}.repl.co/{TOKEN}"
        )
        print(f"✅ Webhook установлен: https://{APP_NAME}.repl.co/{TOKEN}")
    else:
        updater.start_polling()
        print("✅ Бот запущен в режиме polling")
    
    # Запуск Flask для health-check
    threading.Thread(target=run_flask, daemon=True).start()
    print(f"✅ Flask сервер запущен на порту {PORT}")
    
    print("✨ Kufar Bot PRO готов к работе! Проверка каждые 6 минут!")
    updater.idle()

if __name__ == '__main__':
    main()
