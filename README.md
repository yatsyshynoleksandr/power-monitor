# Power Monitor 🔌

Моніторинг наявності електроенергії з повідомленнями в Telegram.

## Як це працює

Скрипт перевіряє доступність двох роутерів:
1. **Роутер 1** — підключений до інтернету, працює від акумулятора
2. **Роутер 2** — підключений за другим роутером, працює від електромережі, доступний через проброшений порт

### Логіка визначення стану

| Роутер 1 (ping) | Роутер 2 (port) | Стан |
|-----------------|-----------------|------|
| ✅ | ✅ | 🟢 Світло є |
| ✅ | ❌ | 🔴 Світла немає |
| ❌ | - | 🟡 Проблеми з інтернетом |

## Можливості

- ✅ Повідомлення про зміну стану в Telegram
- ✅ Тривалість попереднього стану в повідомленнях
- ✅ Щоденні та щотижневі звіти зі статистикою
- ✅ Збереження статистики між перезапусками
- ✅ Ротація логів
- ✅ Graceful shutdown (SIGTERM/SIGINT)
- ✅ Debounce для уникнення хибних спрацювань
- ✅ Retry з exponential backoff для Telegram

## Встановлення

### 1. Залежності

```bash
pip install python-telegram-bot
```

### 2. Створення бота

1. Напишіть [@BotFather](https://t.me/BotFather) в Telegram
2. Створіть нового бота командою `/newbot`
3. Збережіть токен бота

### 3. Налаштування каналу/чату

- Для каналу: додайте бота як адміністратора, використовуйте `@channel_name`
- Для групи: додайте бота в групу, отримайте chat_id через API
- Для особистих повідомлень: напишіть боту, отримайте ваш chat_id

## Конфігурація

Всі параметри налаштовуються через змінні середовища:

| Змінна | Опис | За замовчуванням |
|--------|------|------------------|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram бота | ❌ Обов'язково |
| `TELEGRAM_CHAT_ID` | ID чату/каналу | ❌ Обов'язково |
| `ROUTER_IP` | IP першого роутера | ❌ Обов'язково |
| `ROUTER_2_PORT` | Порт другого роутера | ❌ Обов'язково |
| `CHECK_INTERVAL` | Інтервал перевірки (сек) | `10` |
| `PING_TIMEOUT` | Таймаут ping (сек) | `2` |
| `DEBOUNCE_COUNT` | К-сть підтверджень для зміни стану | `6` |
| `REPORT_HOUR` | Година відправки звітів (статистика за 0:00-24:00) | `7` |
| `LOG_FILE` | Шлях до лог-файлу | `/opt/folder-monitor/power_monitor.log` |
| `LOG_MAX_BYTES` | Макс. розмір лог-файлу | `5242880` (5MB) |
| `LOG_BACKUP_COUNT` | К-сть резервних логів | `3` |
| `STATS_FILE` | Шлях до файлу статистики | `/opt/folder-monitor/power_monitor_stats.json` |
| `TELEGRAM_RETRIES` | К-сть спроб надсилання | `3` |
| `STATS_SAVE_INTERVAL` | Зберігати статистику кожні N ітерацій | `6` |

## Запуск

### Напряму

```bash
export TELEGRAM_BOT_TOKEN="your-bot-token"
export TELEGRAM_CHAT_ID="@your-telegram-chat-id"
export ROUTER_IP="1.2.3.4"
export ROUTER_2_PORT="1234"
python power_monitor.py
```

### В Docker (з даними на хості)

Щоб `LOG_FILE` і `STATS_FILE` зберігались між перезапусками контейнера,
примонтуйте директорію з хоста в `/opt/folder-monitor`.

Це важливо для повного функціоналу:
- ротація логу створює файли `power_monitor.log.1`, `power_monitor.log.2`, ...
- статистика зберігається атомарно через тимчасовий файл `*.tmp` і `replace()`

1. Підготуйте директорію на хості:

```bash
mkdir -p ./data
```

2. Зберіть образ:

```bash
docker build -t power-monitor .
```

3. Запустіть контейнер:

```bash
docker run -d --name power-monitor \
	-e TELEGRAM_BOT_TOKEN="your-bot-token" \
	-e TELEGRAM_CHAT_ID="@your-telegram-chat-id" \
	-e ROUTER_IP="1.2.3.4" \
	-e ROUTER_2_PORT="1234" \
	-v "$(pwd)/data:/opt/folder-monitor" \
	power-monitor
```

У такому випадку змінні `LOG_FILE` і `STATS_FILE` можна не задавати:
скрипт використає дефолтні шляхи `/opt/folder-monitor/...`, які вже вказують
на змонтовану директорію хоста.

Примітка: `REPORT_HOUR` працює у часовому поясі контейнера (часто UTC на EC2).
Якщо потрібен локальний час, налаштуйте timezone контейнера окремо.

#### Docker Compose (опційно)

```yaml
services:
	power-monitor:
		build: .
		container_name: power-monitor
		restart: always
		environment:
			TELEGRAM_BOT_TOKEN: your-bot-token
			TELEGRAM_CHAT_ID: "@your-telegram-chat-id"
			ROUTER_IP: 1.2.3.4
			ROUTER_2_PORT: "1234"
		volumes:
			- ./data:/opt/folder-monitor
```

### Через systemd

Створіть файл `/etc/systemd/system/power-monitor.service`:

```ini
[Unit]
Description=Power Monitor
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/folder-monitor
Environment="TELEGRAM_BOT_TOKEN=your-bot-token"
Environment="TELEGRAM_CHAT_ID=@your-telegram-chat-id"
Environment="ROUTER_IP=1.2.3.4"
Environment="ROUTER_2_PORT=1234"

ExecStart=/usr/bin/python3 /opt/folder-monitor/power_monitor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable power-monitor
sudo systemctl start power-monitor
```

### Перевірка статусу

```bash
sudo systemctl status power-monitor
sudo journalctl -u power-monitor -f
```

## Приклади повідомлень

### Зміна стану

```
🟢 Світло зʼявилось
⏱ Світла не було: 2г 15хв 30с
```

```
🔴 Світло зникло
⏱ Світло було: 5г 42хв 18с
```

```
🌐 Інтернет відновився
⏱ Інтернету не було: 10хв 15с
```

```
☁️ Інтернет пропав (світло є)
⏱ Інтернет був: 3г 20хв 0с
```

### Щоденний/щотижневий звіт

```
📊 Щоденний звіт
📅 28.12 — 29.12

💡 Світло: 18г 45хв 12с (78.1%)
🔌 Без світла: 5г 14хв 48с (21.9%)
📈 Увімкнень: 3

🌐 Інтернет: 23г 58хв 0с (99.9%)
☁️ Без інтернету: 2хв 0с (0.1%)
```

## Структура файлів

```
/opt/folder-monitor/
├── power_monitor.py             # Основний скрипт
├── power_monitor.log            # Лог-файл (з ротацією)
├── power_monitor.log.1          # Резервна копія логу
└── power_monitor_stats.json     # Збережена статистика
```

## Debounce логіка

Щоб уникнути хибних спрацювань через короткочасні збої мережі:

- Стан змінюється тільки після `DEBOUNCE_COUNT` послідовних однакових результатів
- За замовчуванням: 6 перевірок × 10 сек = **60 секунд** стабільного стану

## Збереження статистики

- Статистика зберігається кожну хвилину (~6 ітерацій)
- Зберігається при graceful shutdown
- Автоматично відновлюється при перезапуску
- Атомарний запис (через тимчасовий файл) для захисту від пошкодження

## Звіти

### Щоденний звіт
- **Період:** 0:00 — 24:00 (рівно доба)
- **Скидання статистики:** опівночі (0:00)
- **Відправка:** о `REPORT_HOUR` (за замовчуванням 7:00)

### Щотижневий звіт
- **Період:** понеділок 0:00 — неділя 24:00 (рівно тиждень)
- **Скидання статистики:** понеділок о 0:00
- **Відправка:** понеділок о `REPORT_HOUR`

## Troubleshooting

### Бот не надсилає повідомлення

1. Перевірте токен бота
2. Переконайтесь, що бот доданий в канал/групу як адміністратор
3. Перевірте chat_id (для каналів використовуйте `@channel_name`)

### Часті хибні спрацювання

Збільшіть `DEBOUNCE_COUNT` або `CHECK_INTERVAL`:

```bash
export DEBOUNCE_COUNT=10
export CHECK_INTERVAL=15
```

### Перевірка логів

```bash
tail -f /opt/folder-monitor/power_monitor.log
```
