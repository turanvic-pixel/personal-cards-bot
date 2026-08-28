import asyncio
import datetime
import io
import logging
import os
import re
import uuid
import zipfile

import aiohttp
from aiohttp import web
from PIL import Image, ImageDraw, ImageFont
import imagehash
import pymupdf
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from storage import CardStorage, FavoritesStore, MetaStore, ReminderStore, UserStore, card_file_ids

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("carddeck-bot")


async def with_retries(coro_func, *args, max_attempts: int = 5, **kwargs):
    """Вызывает Telegram API с повторами при флуд-контроле (RetryAfter) и сетевых сбоях,
    вместо того чтобы молча падать при отправке большого числа файлов подряд."""
    attempt = 0
    while True:
        try:
            return await coro_func(*args, **kwargs)
        except RetryAfter as e:
            attempt += 1
            wait = e.retry_after + 1
            logger.warning("Flood control: жду %.1f сек (попытка %s/%s)", wait, attempt, max_attempts)
            await asyncio.sleep(wait)
            if attempt >= max_attempts:
                raise
        except (TimedOut, NetworkError) as e:
            attempt += 1
            logger.warning("Сетевая ошибка: %s (попытка %s/%s)", e, attempt, max_attempts)
            await asyncio.sleep(2 * attempt)
            if attempt >= max_attempts:
                raise


async def send_photo_with_retry(send_call, photo_bytes: bytes, max_attempts: int = 5, **kwargs):
    """Как with_retries, но для отправки фото: пересобирает поток из тех же байт
    на каждой попытке (BytesIO нельзя переиспользовать после частичной отправки)."""
    attempt = 0
    while True:
        try:
            return await send_call(photo=io.BytesIO(photo_bytes), **kwargs)
        except RetryAfter as e:
            attempt += 1
            wait = e.retry_after + 1
            logger.warning("Flood control (фото): жду %.1f сек (попытка %s/%s)", wait, attempt, max_attempts)
            await asyncio.sleep(wait)
            if attempt >= max_attempts:
                raise
        except (TimedOut, NetworkError) as e:
            attempt += 1
            logger.warning("Сетевая ошибка (фото): %s (попытка %s/%s)", e, attempt, max_attempts)
            await asyncio.sleep(2 * attempt)
            if attempt >= max_attempts:
                raise

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ.get("GITHUB_REPO", "carddeck-bot")
EXTERNAL_URL = os.environ.get("EXTERNAL_URL")  # напр. https://carddeck-bot.onrender.com
PORT = int(os.environ.get("PORT", 10000))
OLD_BOT_TOKEN = os.environ.get("OLD_BOT_TOKEN")  # опционально: токен бота-источника для команды /import_cards

storage = CardStorage(GITHUB_TOKEN, GITHUB_REPO)
favorites = FavoritesStore(GITHUB_TOKEN, GITHUB_REPO)
reminders = ReminderStore(GITHUB_TOKEN, GITHUB_REPO)
users = UserStore(GITHUB_TOKEN, GITHUB_REPO)
meta = MetaStore(GITHUB_TOKEN, GITHUB_REPO)

# Меняй эту строку при каждом изменении набора кнопок внизу экрана —
# бот сам один раз попросит всех известных пользователей написать любое слово,
# чтобы у них обновилась клавиатура.
KEYBOARD_VERSION = "v6-mode-selector"

REMINDER_BUTTON_TEXT = "⏰ Напоминания"
FAVORITES_BUTTON_TEXT = "⭐ Избранное"
STATS_BUTTON_TEXT = "📊 Статистика"
TEXT_CARD_BUTTON_TEXT = "✍️ Текстовая карточка"
ADMIN_MENU_BUTTON_TEXT = "🛠 Управление"
MODE_BUTTON_TEXT = "🔀 Режим показа"

DRAW_BUTTON = ReplyKeyboardMarkup(
    [
        ["💎 Открыть жемчужину души"],
        [MODE_BUTTON_TEXT],
        [FAVORITES_BUTTON_TEXT, STATS_BUTTON_TEXT],
        [REMINDER_BUTTON_TEXT, TEXT_CARD_BUTTON_TEXT],
        [ADMIN_MENU_BUTTON_TEXT],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# статистика просмотров — в памяти процесса (как и очередь карточек), сбрасывается при рестарте Render
_stats_data: dict[int, dict[str, int]] = {}


def record_view(user_id: int):
    today = datetime.date.today().isoformat()
    user_stats = _stats_data.setdefault(user_id, {})
    user_stats[today] = user_stats.get(today, 0) + 1


def get_stats(user_id: int):
    today = datetime.date.today()
    user_stats = _stats_data.get(user_id, {})
    total = sum(user_stats.values())
    day = user_stats.get(today.isoformat(), 0)
    week_start = today - datetime.timedelta(days=today.weekday())
    week = sum(
        v for d, v in user_stats.items()
        if week_start <= datetime.date.fromisoformat(d) <= today
    )
    month = sum(
        v for d, v in user_stats.items()
        if datetime.date.fromisoformat(d).year == today.year
        and datetime.date.fromisoformat(d).month == today.month
    )
    return total, day, week, month


def _admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔎 Показать карточку по номеру", callback_data="menu_view")],
            [InlineKeyboardButton("✏️ Редактировать карточку", callback_data="menu_edit")],
            [InlineKeyboardButton("🔗 Объединить карточки", callback_data="menu_merge")],
            [InlineKeyboardButton("📋 Список номеров карточек", callback_data="menu_count")],
            [InlineKeyboardButton("🗑 Удалить карточку", callback_data="menu_delete")],
            [InlineKeyboardButton("📦 Скачать карточки", callback_data="menu_export")],
            [InlineKeyboardButton("🔁 Досчитать хэши (проверка дублей)", callback_data="menu_hashmissing")],
        ]
    )


async def admin_menu_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("Что нужно сделать?", reply_markup=_admin_menu_keyboard())


async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer()
        return
    await query.answer()
    action = query.data
    if action == "menu_count":
        await count_cmd(update, context)
    elif action == "menu_view":
        context.user_data["awaiting_view_number"] = True
        await query.message.reply_text("Пришли номер карточки, которую хочешь посмотреть.")
    elif action == "menu_delete":
        context.user_data["awaiting_delete_number"] = True
        await query.message.reply_text("Пришли номер карточки, которую нужно удалить.")
    elif action == "menu_edit":
        context.user_data["awaiting_edit_number"] = True
        await query.message.reply_text("Пришли номер карточки, которую нужно отредактировать.")
    elif action == "menu_merge":
        context.user_data["awaiting_merge_numbers"] = True
        await query.message.reply_text(
            "Пришли номера карточек в том порядке, в котором их нужно объединить, через запятую (например: 5,3,11)."
        )
    elif action == "menu_export":
        context.user_data["awaiting_export_selection"] = True
        await query.message.reply_text(
            "Какие карточки скачать? Пришли номера через запятую (например 5,7,12-15) или напиши «все»."
        )
    elif action == "menu_hashmissing":
        await hash_missing_cmd(update, context)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total, day, week, month = get_stats(update.effective_user.id)
    await update.message.reply_text(
        "📊 Твоя статистика просмотров:\n\n"
        f"Всего: {total}\n"
        f"За день: {day}\n"
        f"За неделю: {week}\n"
        f"За месяц: {month}\n\n"
        f"Карточек в боте всего: {storage.count()}"
    )


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        users.add(update.effective_user.id)


async def broadcast_keyboard_update(application: Application):
    text = "У бота обновились кнопки! Напиши любое слово, чтобы увидеть новое меню 🙂"
    for uid in users.all():
        try:
            await application.bot.send_message(chat_id=uid, text=text)
        except Exception as e:
            logger.warning("Не удалось уведомить пользователя %s: %s", uid, e)
        await asyncio.sleep(0.05)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "Привет! Кнопка внизу экрана всегда под рукой — жми и вытягивай карточку.",
        reply_markup=DRAW_BUTTON,
    )
    try:
        chat = await context.bot.get_chat(update.effective_chat.id)
        if chat.pinned_message is None:
            await context.bot.pin_chat_message(
                chat_id=update.effective_chat.id, message_id=msg.message_id, disable_notification=True
            )
    except Exception as e:
        logger.warning("Не удалось закрепить сообщение: %s", e)


def _fav_keyboard(card_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❤️ Сохранить", callback_data=f"fav:{card_id}")]])


def _mode_keyboard(user_id: int) -> InlineKeyboardMarkup:
    current = storage.get_mode(user_id)
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(("✅ " if current == "random" else "") + "🎲 Случайно", callback_data="mode:random")],
            [InlineKeyboardButton("🔢 По порядку с начала", callback_data="mode:seq_restart")],
            [InlineKeyboardButton(("✅ " if current == "sequential" else "") + "▶️ Продолжить по порядку", callback_data="mode:seq_continue")],
        ]
    )


async def mode_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Как показывать карточки?", reply_markup=_mode_keyboard(update.effective_user.id)
    )


async def mode_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    user_id = update.effective_user.id
    if action == "random":
        storage.set_mode(user_id, "random")
        await query.message.reply_text("Режим: 🎲 случайные карточки без повторов.", reply_markup=DRAW_BUTTON)
    elif action == "seq_restart":
        storage.set_mode(user_id, "sequential")
        storage.reset_sequential(user_id)
        await query.message.reply_text("Режим: 🔢 по порядку номеров, начинаем сначала.", reply_markup=DRAW_BUTTON)
    elif action == "seq_continue":
        storage.set_mode(user_id, "sequential")
        await query.message.reply_text("Режим: ▶️ по порядку номеров, продолжаем с прошлого места.", reply_markup=DRAW_BUTTON)


async def draw_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    mode = storage.get_mode(user_id)
    card = storage.next_sequential_card(user_id) if mode == "sequential" else storage.next_card_for_user(user_id)
    if card is None:
        await update.message.reply_text("Пока нет ни одной карточки в коллекции.", reply_markup=DRAW_BUTTON)
        return
    record_view(user_id)
    caption = f"Карточка #{card['id']}" if update.effective_user.id == ADMIN_ID else None
    kb = _fav_keyboard(card["id"])
    file_ids = card_file_ids(card)
    for i, fid in enumerate(file_ids):
        is_last = i == len(file_ids) - 1
        if card.get("kind") == "document":
            await update.message.reply_document(
                document=fid, caption=caption if is_last else None, reply_markup=kb if is_last else None
            )
        else:
            await update.message.reply_photo(
                photo=fid, caption=caption if is_last else None, reply_markup=kb if is_last else None
            )


async def favorite_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    card_id = int(query.data.split(":")[1])
    added = favorites.add(update.effective_user.id, card_id)
    await query.answer("Сохранено в избранное ❤️" if added else "Уже в избранном")


async def unfavorite_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    card_id = int(query.data.split(":")[1])
    favorites.remove(update.effective_user.id, card_id)
    await query.answer("Убрано из избранного")
    await query.edit_message_reply_markup(reply_markup=None)


async def favorites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ids = favorites.list_for_user(update.effective_user.id)
    if not ids:
        await update.message.reply_text("Пока нет сохранённых карточек. Жми ❤️ Сохранить под карточкой.")
        return
    await update.message.reply_text(f"Сохранено карточек: {len(ids)}. Отправляю (до 20 за раз)...")
    for card_id in ids[:20]:
        card = next((c for c in storage.cards if c["id"] == card_id), None)
        if card is None:
            continue
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Убрать из избранного", callback_data=f"unfav:{card_id}")]])
        file_ids = card_file_ids(card)
        for i, fid in enumerate(file_ids):
            is_last = i == len(file_ids) - 1
            if card.get("kind") == "document":
                await update.message.reply_document(document=fid, reply_markup=kb if is_last else None)
            else:
                await update.message.reply_photo(photo=fid, reply_markup=kb if is_last else None)
    if len(ids) > 20:
        await update.message.reply_text(f"И ещё {len(ids) - 20} в избранном — вызови /favorites ещё раз позже.")


MAX_IMAGE_DIMENSION = 2000
JPEG_QUALITY = 90


def optimize_image_bytes(raw_bytes: bytes) -> bytes:
    """Мягкое сжатие: сохраняет резкость текста лучше, чем стандартное сжатие Telegram-фото,
    и при этом отправляется как «Фото» — без плашки с именем файла."""
    img = Image.open(io.BytesIO(raw_bytes))
    img = img.convert("RGB")
    if max(img.size) > MAX_IMAGE_DIMENSION:
        img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def render_pdf_all_pages(pdf_bytes: bytes) -> list:
    """Рендерит все страницы PDF в PNG-байты нужного разрешения, по порядку."""
    pdf = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        pages = []
        for page in pdf:
            zoom = MAX_IMAGE_DIMENSION / max(page.rect.width, page.rect.height)
            matrix = pymupdf.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=matrix)
            pages.append(pix.tobytes("png"))
        return pages
    finally:
        pdf.close()


import hashlib


TEXT_CARD_SIZE = (1200, 1600)
FONT_PATH = os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf")
TEXT_CARD_MIN_FONT_SIZE = 32  # меньше не уменьшаем — плохо читается; вместо этого переносим на новую страницу


def _wrap_by_pixel_width(draw, text: str, font, max_width: int) -> list:
    """Переносит строки по фактической ширине в пикселях, а не по числу символов —
    иначе строки обрываются рано и справа остаётся пустое место."""
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=font) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _render_text_page(lines: list, font, line_height: int, total_height: int) -> bytes:
    width, height = TEXT_CARD_SIZE
    bg_color = (240, 233, 220)
    text_color = (40, 34, 28)
    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)
    y = (height - total_height) // 2
    for line in lines:
        line_width = draw.textlength(line, font=font)
        x = (width - line_width) / 2
        draw.text((x, y), line, font=font, fill=text_color)
        y += line_height
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue()


def render_text_card_pages(text: str) -> list:
    """Оформляет текст в одну или несколько карточек-страниц: тёплый фон, текст по центру.
    Короткий текст крупно заполняет одну карточку. Длинный текст, который не помещается даже
    при минимальном читаемом размере шрифта, автоматически переносится на следующие страницы —
    как страницы PDF, показываются потом одна за другой."""
    width, height = TEXT_CARD_SIZE
    margin = 90
    max_text_width = width - 2 * margin
    max_text_height = height - 2 * margin

    tmp_img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(tmp_img)

    for font_size in range(220, TEXT_CARD_MIN_FONT_SIZE - 1, -2):
        font = ImageFont.truetype(FONT_PATH, font_size)
        lines = _wrap_by_pixel_width(draw, text, font, max_text_width)
        line_height = int(font_size * 1.35)
        total_height = line_height * len(lines)
        max_line_width = max((draw.textlength(line, font=font) for line in lines), default=0)
        if total_height <= max_text_height and max_line_width <= max_text_width:
            return [_render_text_page(lines, font, line_height, total_height)]

    # даже на минимальном размере весь текст не влезает в одну страницу — разбиваем на несколько
    font = ImageFont.truetype(FONT_PATH, TEXT_CARD_MIN_FONT_SIZE)
    all_lines = _wrap_by_pixel_width(draw, text, font, max_text_width)
    line_height = int(TEXT_CARD_MIN_FONT_SIZE * 1.35)
    lines_per_page = max(1, max_text_height // line_height)
    pages = []
    for i in range(0, len(all_lines), lines_per_page):
        page_lines = all_lines[i : i + lines_per_page]
        page_total_height = line_height * len(page_lines)
        pages.append(_render_text_page(page_lines, font, line_height, page_total_height))
    return pages


def compute_content_hash(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def compute_phash(raw_bytes: bytes) -> str | None:
    try:
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        return str(imagehash.phash(img))
    except Exception as e:
        logger.warning("Не удалось посчитать хэш картинки: %s", e)
        return None


# Буфер альбомов: несколько фото/файлов, присланных одним альбомом (media_group_id),
# собираются в одну карточку — как страницы PDF, показываются потом одна за другой.
_media_group_buffers: dict = {}
_pending_duplicate_adds: dict = {}


async def _ask_duplicate_confirmation(bot, chat_id, existing_card: dict, pending_data: dict):
    """Показывает найденную похожую карточку и спрашивает — правда дубликат, или добавить всё равно."""
    key = uuid.uuid4().hex[:12]
    pending_data["chat_id"] = chat_id
    _pending_duplicate_adds[key] = pending_data
    caption = f"Похоже на карточку #{existing_card['id']}. Это дубликат?"
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Да, добавить новую", callback_data=f"forceadd:{key}"),
            InlineKeyboardButton("❌ Не добавлять", callback_data=f"skipadd:{key}"),
        ]]
    )
    existing_ids = card_file_ids(existing_card)
    for i, fid in enumerate(existing_ids):
        is_last = i == len(existing_ids) - 1
        if existing_card.get("kind") == "document":
            await bot.send_document(chat_id=chat_id, document=fid, caption=caption if is_last else None, reply_markup=kb if is_last else None)
        else:
            await bot.send_photo(chat_id=chat_id, photo=fid, caption=caption if is_last else None, reply_markup=kb if is_last else None)


async def duplicate_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer()
        return
    await query.answer()
    action, key = query.data.split(":", 1)
    pending = _pending_duplicate_adds.pop(key, None)
    if not pending:
        await query.message.reply_text("Эта карточка уже обработана или устарела.", reply_markup=DRAW_BUTTON)
        return
    if action == "skipadd":
        await query.message.reply_text("Не добавляю.", reply_markup=DRAW_BUTTON)
        return

    if pending.get("is_text"):
        pages = render_text_card_pages(pending["text"])
        file_ids = []
        for page_bytes in pages:
            sent = await context.bot.send_photo(chat_id=pending["chat_id"], photo=io.BytesIO(page_bytes))
            file_ids.append(sent.photo[-1].file_id)
        if len(file_ids) == 1:
            new_id = storage.add_card(file_ids[0], kind="photo", content_hash=pending.get("content_hash"))
        else:
            new_id = storage.add_multi_card(file_ids, kind="photo", content_hash=pending.get("content_hash"))
    elif pending.get("file_ids") is not None:
        new_id = storage.add_multi_card(
            pending["file_ids"], kind=pending.get("kind", "photo"),
            phash=pending.get("phash"), content_hash=pending.get("content_hash"),
        )
    else:
        new_id = storage.add_card(
            pending["file_id"], kind=pending.get("kind", "photo"),
            phash=pending.get("phash"), content_hash=pending.get("content_hash"),
        )
    await query.message.reply_text(
        f"Добавлено! Карточка #{new_id}. Всего карточек: {storage.count()}.", reply_markup=DRAW_BUTTON
    )


_pending_group_choices: dict = {}


async def _flush_media_group(context: ContextTypes.DEFAULT_TYPE):
    group_id = context.job.data["group_id"]
    buf = _media_group_buffers.pop(group_id, None)
    if not buf or not buf["file_ids"]:
        return
    chat_id = buf["chat_id"]
    file_ids = buf["file_ids"]
    kind = buf["kind"]
    editing_id = buf.get("editing_card_id")
    phash = compute_phash(buf["first_raw"]) if buf.get("first_raw") else None
    content_hash = compute_content_hash(buf["first_raw"]) if buf.get("first_raw") else None

    if editing_id:
        if len(file_ids) == 1:
            ok = storage.update_card(editing_id, file_id=file_ids[0], kind=kind, phash=phash, content_hash=content_hash)
        else:
            ok = storage.update_card(editing_id, file_ids=file_ids, kind=kind, phash=phash, content_hash=content_hash)
        msg = f"Карточка #{editing_id} обновлена ({len(file_ids)} фото)." if ok else f"Не нашла карточку #{editing_id} — возможно, её удалили."
        await context.bot.send_message(chat_id=chat_id, text=msg, reply_markup=DRAW_BUTTON)
        return

    if len(file_ids) == 1:
        dup = storage.find_duplicate(content_hash=content_hash)
        if dup:
            await _ask_duplicate_confirmation(
                context.bot, chat_id, dup,
                {"file_id": file_ids[0], "kind": kind, "phash": phash, "content_hash": content_hash},
            )
            return
        new_id = storage.add_card(file_ids[0], kind=kind, phash=phash, content_hash=content_hash)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Добавлено! Карточка #{new_id}. Всего карточек: {storage.count()}.",
            reply_markup=DRAW_BUTTON,
        )
        return

    # несколько фото, не редактирование — спрашиваем, как сохранить, а не решаем автоматически
    _pending_group_choices[group_id] = buf
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(f"📑 Отдельными ({len(file_ids)})", callback_data=f"splitgroup:{group_id}"),
            InlineKeyboardButton(f"📖 Одной карточкой ({len(file_ids)} стр.)", callback_data=f"combinegroup:{group_id}"),
        ]]
    )
    await context.bot.send_message(
        chat_id=chat_id, text=f"Прислано {len(file_ids)} фото. Как сохранить?", reply_markup=kb
    )


async def group_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer()
        return
    await query.answer()
    action, key = query.data.split(":", 1)
    buf = _pending_group_choices.pop(key, None)
    if not buf:
        await query.message.reply_text("Эти карточки уже обработаны или устарели.", reply_markup=DRAW_BUTTON)
        return
    file_ids = buf["file_ids"]
    kind = buf["kind"]
    phash = compute_phash(buf["first_raw"]) if buf.get("first_raw") else None
    content_hash = compute_content_hash(buf["first_raw"]) if buf.get("first_raw") else None

    if action == "splitgroup":
        first_fid = file_ids[0]
        rest = file_ids[1:]
        added = 0
        for fid in rest:
            storage.add_card(fid, kind=kind)
            added += 1
        dup = storage.find_duplicate(content_hash=content_hash) if content_hash else None
        if dup:
            note = f" Остальные {added} добавлены сразу." if added else ""
            await query.message.reply_text(f"Первая из присланных похожа на существующую карточку.{note}", reply_markup=DRAW_BUTTON)
            await _ask_duplicate_confirmation(
                context.bot, query.message.chat.id, dup,
                {"file_id": first_fid, "kind": kind, "phash": phash, "content_hash": content_hash},
            )
            return
        storage.add_card(first_fid, kind=kind, phash=phash, content_hash=content_hash)
        added += 1
        msg = f"Добавлено отдельными карточками: {added}. Всего карточек: {storage.count()}."
        await query.message.reply_text(msg, reply_markup=DRAW_BUTTON)
        return

    # combinegroup
    dup = storage.find_duplicate(content_hash=content_hash) if content_hash else None
    if dup:
        await _ask_duplicate_confirmation(
            context.bot, query.message.chat.id, dup,
            {"file_ids": file_ids, "kind": kind, "phash": phash, "content_hash": content_hash},
        )
        return
    new_id = storage.add_multi_card(file_ids, kind=kind, phash=phash, content_hash=content_hash)
    await query.message.reply_text(
        f"Добавлено одной карточкой ({len(file_ids)} стр.)! Карточка #{new_id}. Всего карточек: {storage.count()}.",
        reply_markup=DRAW_BUTTON,
    )


def _schedule_media_group_flush(context: ContextTypes.DEFAULT_TYPE, group_id: str):
    for job in context.application.job_queue.get_jobs_by_name(f"mg_{group_id}"):
        job.schedule_removal()
    context.application.job_queue.run_once(
        _flush_media_group, when=1.5, data={"group_id": group_id}, name=f"mg_{group_id}"
    )


async def admin_add_card_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        photo = update.message.photo[-1]
        group_id = update.message.media_group_id
        if group_id:
            is_new = group_id not in _media_group_buffers
            buf = _media_group_buffers.setdefault(
                group_id, {"file_ids": [], "kind": "photo", "chat_id": update.effective_chat.id}
            )
            if is_new:
                buf["editing_card_id"] = context.user_data.pop("editing_card_id", None)
                try:
                    tg_file = await with_retries(context.bot.get_file, photo.file_id)
                    buf["first_raw"] = bytes(await with_retries(tg_file.download_as_bytearray))
                except Exception:
                    logger.exception("Не удалось скачать первое фото альбома")
            buf["file_ids"].append(photo.file_id)
            _schedule_media_group_flush(context, group_id)
            return

        tg_file = await with_retries(context.bot.get_file, photo.file_id)
        raw = await with_retries(tg_file.download_as_bytearray)
        phash = compute_phash(bytes(raw))
        content_hash = compute_content_hash(bytes(raw))
        editing_id = context.user_data.pop("editing_card_id", None)
        if editing_id:
            ok = storage.update_card(editing_id, file_id=photo.file_id, kind="photo", phash=phash, content_hash=content_hash)
            msg = f"Карточка #{editing_id} обновлена." if ok else f"Не нашла карточку #{editing_id} — возможно, её удалили."
            await update.message.reply_text(msg, reply_markup=DRAW_BUTTON)
            return
        dup = storage.find_duplicate(content_hash=content_hash)
        if dup:
            await _ask_duplicate_confirmation(
                context.bot, update.effective_chat.id, dup,
                {"file_id": photo.file_id, "kind": "photo", "phash": phash, "content_hash": content_hash},
            )
            return
        new_id = storage.add_card(photo.file_id, kind="photo", phash=phash, content_hash=content_hash)
        await update.message.reply_text(
            f"Добавлено! Карточка #{new_id}. Всего карточек: {storage.count()}.",
            reply_markup=DRAW_BUTTON,
        )
    except Exception as e:
        logger.exception("Ошибка при добавлении фото-карточки")
        await update.message.reply_text(f"Не удалось добавить карточку: {e}", reply_markup=DRAW_BUTTON)


async def admin_add_card_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        doc = update.message.document
        tg_file = await with_retries(context.bot.get_file, doc.file_id)
        raw = await with_retries(tg_file.download_as_bytearray)
        try:
            pages = render_pdf_all_pages(bytes(raw))
        except Exception as e:
            logger.warning("Не удалось отрендерить PDF: %s", e)
            await update.message.reply_text("Не получилось прочитать этот PDF, попробуй другой файл.")
            return
        if not pages:
            await update.message.reply_text("В этом PDF не нашлось страниц.")
            return
        phash = compute_phash(pages[0])
        content_hash = compute_content_hash(bytes(raw))
        editing_id = context.user_data.pop("editing_card_id", None)
        dup = None
        if not editing_id:
            dup = storage.find_duplicate(content_hash=content_hash)
        file_ids = []
        for page_bytes in pages:
            try:
                optimized = optimize_image_bytes(page_bytes)
            except Exception:
                optimized = page_bytes
            sent = await send_photo_with_retry(
                lambda **kw: context.bot.send_photo(chat_id=ADMIN_ID, **kw), optimized
            )
            file_ids.append(sent.photo[-1].file_id)
        if dup:
            pending = {"kind": "photo", "phash": phash, "content_hash": content_hash}
            if len(file_ids) == 1:
                pending["file_id"] = file_ids[0]
            else:
                pending["file_ids"] = file_ids
            await _ask_duplicate_confirmation(context.bot, update.effective_chat.id, dup, pending)
            return
        if editing_id:
            if len(file_ids) == 1:
                ok = storage.update_card(editing_id, file_id=file_ids[0], kind="photo", phash=phash, content_hash=content_hash)
            else:
                ok = storage.update_card(editing_id, file_ids=file_ids, kind="photo", phash=phash, content_hash=content_hash)
            msg = f"Карточка #{editing_id} обновлена ({len(file_ids)} стр.)." if ok else f"Не нашла карточку #{editing_id} — возможно, её удалили."
            await update.message.reply_text(msg, reply_markup=DRAW_BUTTON)
            return
        if len(file_ids) == 1:
            new_id = storage.add_card(file_ids[0], kind="photo", phash=phash, content_hash=content_hash)
        else:
            new_id = storage.add_multi_card(file_ids, kind="photo", phash=phash, content_hash=content_hash)
        await update.message.reply_text(
            f"Добавлено из PDF ({len(file_ids)} стр.)! Карточка #{new_id}. Всего карточек: {storage.count()}.",
            reply_markup=DRAW_BUTTON,
        )
    except Exception as e:
        logger.exception("Ошибка при добавлении PDF-карточки")
        await update.message.reply_text(f"Не удалось добавить карточку из PDF: {e}", reply_markup=DRAW_BUTTON)


async def admin_add_card_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    doc = update.message.document
    if not doc.mime_type or not doc.mime_type.startswith("image/"):
        return
    try:
        tg_file = await with_retries(context.bot.get_file, doc.file_id)
        raw = await with_retries(tg_file.download_as_bytearray)
        group_id = update.message.media_group_id

        try:
            optimized = optimize_image_bytes(bytes(raw))
        except Exception as e:
            logger.warning("Не удалось обработать картинку: %s", e)
            await update.message.reply_text("Не получилось обработать эту картинку, попробуй другой файл.")
            return
        sent = await send_photo_with_retry(update.message.reply_photo, optimized)
        photo_file_id = sent.photo[-1].file_id

        if group_id:
            is_new = group_id not in _media_group_buffers
            buf = _media_group_buffers.setdefault(
                group_id, {"file_ids": [], "kind": "photo", "chat_id": update.effective_chat.id}
            )
            if is_new:
                buf["editing_card_id"] = context.user_data.pop("editing_card_id", None)
                buf["first_raw"] = bytes(raw)
            buf["file_ids"].append(photo_file_id)
            _schedule_media_group_flush(context, group_id)
            return

        phash = compute_phash(bytes(raw))
        content_hash = compute_content_hash(bytes(raw))
        editing_id = context.user_data.get("editing_card_id")
        if editing_id:
            context.user_data.pop("editing_card_id", None)
            ok = storage.update_card(editing_id, file_id=photo_file_id, kind="photo", phash=phash, content_hash=content_hash)
            msg = f"Карточка #{editing_id} обновлена." if ok else f"Не нашла карточку #{editing_id} — возможно, её удалили."
            await update.message.reply_text(msg, reply_markup=DRAW_BUTTON)
            return
        dup = storage.find_duplicate(content_hash=content_hash)
        if dup:
            await _ask_duplicate_confirmation(
                context.bot, update.effective_chat.id, dup,
                {"file_id": photo_file_id, "kind": "photo", "phash": phash, "content_hash": content_hash},
            )
            return
        new_id = storage.add_card(photo_file_id, kind="photo", phash=phash, content_hash=content_hash)
        await update.message.reply_text(
            f"Добавлено (оптимизировано)! Карточка #{new_id}. Всего карточек: {storage.count()}.",
            reply_markup=DRAW_BUTTON,
        )
    except Exception as e:
        logger.exception("Ошибка при добавлении карточки из файла")
        await update.message.reply_text(f"Не удалось добавить карточку: {e}", reply_markup=DRAW_BUTTON)


async def reprocess_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    doc_cards = [c for c in storage.cards if c.get("kind") == "document"]
    if not doc_cards:
        await update.message.reply_text("Все карточки уже в едином формате, пересобирать нечего.")
        return
    await update.message.reply_text(f"Пересобираю {len(doc_cards)} карточек, это может занять время...")
    fixed = 0
    failed = []
    for card in doc_cards:
        try:
            tg_file = await context.bot.get_file(card["file_id"])
            raw = await tg_file.download_as_bytearray()
            phash = compute_phash(bytes(raw))
            content_hash = compute_content_hash(bytes(raw))
            optimized = optimize_image_bytes(bytes(raw))
            sent = await context.bot.send_photo(chat_id=ADMIN_ID, photo=io.BytesIO(optimized))
            card["file_id"] = sent.photo[-1].file_id
            card["kind"] = "photo"
            if phash:
                card["phash"] = phash
            card["content_hash"] = content_hash
            fixed += 1
        except Exception as e:
            logger.warning("Не удалось пересобрать карточку #%s: %s", card["id"], e)
            failed.append(card["id"])
    if fixed > 0:
        storage.persist(f"reprocess {fixed} document-cards into optimized photo cards")
    msg = f"Готово! Пересобрано: {fixed}."
    if failed:
        msg += f" Не получилось: {failed}."
    await update.message.reply_text(msg, reply_markup=DRAW_BUTTON)


async def import_cards_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переносит карточки из другого бота: file_id в Telegram привязаны к конкретному боту,
    поэтому просто скопировать cards.json между ботами недостаточно — картинки нужно
    скачать через старого бота (OLD_BOT_TOKEN) и перезалить уже от имени этого бота."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not OLD_BOT_TOKEN:
        await update.message.reply_text("Переменная OLD_BOT_TOKEN не задана — переносить неоткуда.")
        return
    old_bot = Bot(token=OLD_BOT_TOKEN)
    await update.message.reply_text(f"Переношу {storage.count()} карточек из старого бота, это займёт время...")
    fixed = 0
    failed = []
    for card in storage.cards:
        try:
            tg_file = await old_bot.get_file(card["file_id"])
            raw = bytes(await tg_file.download_as_bytearray())
            phash = compute_phash(raw)
            content_hash = compute_content_hash(raw)
            try:
                optimized = optimize_image_bytes(raw)
            except Exception:
                optimized = raw
            sent = await context.bot.send_photo(chat_id=ADMIN_ID, photo=io.BytesIO(optimized))
            card["file_id"] = sent.photo[-1].file_id
            card["kind"] = "photo"
            if phash:
                card["phash"] = phash
            card["content_hash"] = content_hash
            fixed += 1
        except Exception as e:
            logger.warning("Не удалось перенести карточку #%s: %s", card["id"], e)
            failed.append(card["id"])
    if fixed > 0:
        storage.persist(f"import {fixed} cards from old bot via /import_cards")
    msg = f"Готово! Перенесено: {fixed}."
    if failed:
        msg += f" Не получилось: {failed}."
    await update.message.reply_text(msg, reply_markup=DRAW_BUTTON)


async def hash_missing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    missing = [c for c in storage.cards if not c.get("content_hash")]
    if not missing:
        await update.message.reply_text("У всех карточек уже есть хэш для проверки дублей.")
        return
    await update.message.reply_text(f"Считаю хэш для {len(missing)} карточек...")
    fixed = 0
    failed = []
    for card in missing:
        try:
            first_file_id = card_file_ids(card)[0]
            tg_file = await context.bot.get_file(first_file_id)
            raw = await tg_file.download_as_bytearray()
            content_hash = compute_content_hash(bytes(raw))
            phash = compute_phash(bytes(raw))
            card["content_hash"] = content_hash
            if phash:
                card["phash"] = phash
            fixed += 1
        except Exception as e:
            logger.warning("Не удалось посчитать хэш для карточки #%s: %s", card["id"], e)
            failed.append(card["id"])
    if fixed > 0:
        storage.persist(f"backfill content_hash for {fixed} cards")
    msg = f"Готово! Хэш посчитан для {fixed} карточек."
    if failed:
        msg += f" Не получилось: {failed}."
    await update.message.reply_text(msg, reply_markup=DRAW_BUTTON)


async def text_card_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data["awaiting_text_card"] = True
    await update.message.reply_text("Пришли текст — я оформлю его в карточку.", reply_markup=DRAW_BUTTON)


def _delete_confirm_keyboard(card_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirmdel:{card_id}"),
            InlineKeyboardButton("❌ Отмена", callback_data="canceldel"),
        ]]
    )


async def _prompt_delete_confirmation(message, card_id: int):
    card = next((c for c in storage.cards if c["id"] == card_id), None)
    if card is None:
        await message.reply_text(f"Карточка #{card_id} не найдена.", reply_markup=DRAW_BUTTON)
        return
    caption = f"Удалить карточку #{card_id}?"
    kb = _delete_confirm_keyboard(card_id)
    file_ids = card_file_ids(card)
    for i, fid in enumerate(file_ids):
        is_last = i == len(file_ids) - 1
        if card.get("kind") == "document":
            await message.reply_document(document=fid, caption=caption if is_last else None, reply_markup=kb if is_last else None)
        else:
            await message.reply_photo(photo=fid, caption=caption if is_last else None, reply_markup=kb if is_last else None)


async def delete_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer()
        return
    await query.answer()
    if query.data == "canceldel":
        await query.message.reply_text("Отменено, карточка на месте.", reply_markup=DRAW_BUTTON)
        return
    card_id = int(query.data.split(":")[1])
    ok = storage.delete_card(card_id)
    if ok:
        await query.message.reply_text(
            f"Карточка #{card_id} удалена. Всего карточек: {storage.count()}.", reply_markup=DRAW_BUTTON
        )
    else:
        await query.message.reply_text(f"Карточка #{card_id} не найдена.", reply_markup=DRAW_BUTTON)


async def admin_add_card_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if context.user_data.get("awaiting_merge_numbers"):
        context.user_data["awaiting_merge_numbers"] = False
        text = (update.message.text or "").strip()
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if len(parts) < 2 or not all(p.isdigit() for p in parts):
            await update.message.reply_text(
                "Нужно минимум два номера через запятую, например: 5,3,11.", reply_markup=DRAW_BUTTON
            )
            return
        ids_in_order = [int(p) for p in parts]
        if len(set(ids_in_order)) != len(ids_in_order):
            await update.message.reply_text("В списке есть повторяющиеся номера — пришли ещё раз без повторов.", reply_markup=DRAW_BUTTON)
            return
        cards_in_order = []
        missing = []
        for cid in ids_in_order:
            card = next((c for c in storage.cards if c["id"] == cid), None)
            if card is None:
                missing.append(cid)
            else:
                cards_in_order.append(card)
        if missing:
            await update.message.reply_text(f"Не нашла карточки: {missing}. Ничего не объединяю.", reply_markup=DRAW_BUTTON)
            return
        combined_file_ids = []
        for card in cards_in_order:
            combined_file_ids.extend(card_file_ids(card))
        kind = cards_in_order[0].get("kind", "photo")
        new_id = storage.add_multi_card(combined_file_ids, kind=kind)
        for cid in ids_in_order:
            storage.delete_card(cid)
        await update.message.reply_text(
            f"Объединено! Новая карточка #{new_id} ({len(combined_file_ids)} стр.) из карточек {ids_in_order}. "
            f"Всего карточек: {storage.count()}.",
            reply_markup=DRAW_BUTTON,
        )
        return
    if context.user_data.get("awaiting_export_selection"):
        context.user_data["awaiting_export_selection"] = False
        text = update.message.text or ""
        selected_ids = parse_card_selection(text, storage.list_ids())
        cards_to_export = [c for c in storage.cards if c["id"] in set(selected_ids)]
        await _export_cards(update.message, context, cards_to_export)
        return
    if context.user_data.get("awaiting_view_number"):
        context.user_data["awaiting_view_number"] = False
        text = (update.message.text or "").strip()
        if not text.isdigit():
            await update.message.reply_text("Это не похоже на номер карточки. Попробуй ещё раз через меню.", reply_markup=DRAW_BUTTON)
            return
        await _reply_with_card(update.message, int(text))
        return
    if context.user_data.get("awaiting_delete_number"):
        context.user_data["awaiting_delete_number"] = False
        text = (update.message.text or "").strip()
        if not text.isdigit():
            await update.message.reply_text("Это не похоже на номер карточки. Попробуй ещё раз через меню.", reply_markup=DRAW_BUTTON)
            return
        await _prompt_delete_confirmation(update.message, int(text))
        return
    if context.user_data.get("awaiting_edit_number"):
        context.user_data["awaiting_edit_number"] = False
        text = (update.message.text or "").strip()
        if not text.isdigit() or not any(c["id"] == int(text) for c in storage.cards):
            await update.message.reply_text("Карточка с таким номером не найдена. Попробуй ещё раз через меню.", reply_markup=DRAW_BUTTON)
            return
        card_id = int(text)
        context.user_data["editing_card_id"] = card_id
        await update.message.reply_text(
            f"Пришли новое содержимое (фото, файл или текст) для карточки #{card_id}.", reply_markup=DRAW_BUTTON
        )
        return
    if context.user_data.get("editing_card_id"):
        card_id = context.user_data.pop("editing_card_id")
        text = update.message.text
        if not text or not text.strip():
            return
        try:
            content_hash = compute_content_hash(text.strip().encode("utf-8"))
            pages = render_text_card_pages(text)
            file_ids = []
            for page_bytes in pages:
                sent = await send_photo_with_retry(update.message.reply_photo, page_bytes)
                file_ids.append(sent.photo[-1].file_id)
            if len(file_ids) == 1:
                ok = storage.update_card(card_id, file_id=file_ids[0], kind="photo", content_hash=content_hash)
            else:
                ok = storage.update_card(card_id, file_ids=file_ids, kind="photo", content_hash=content_hash)
            if ok:
                await update.message.reply_text(f"Карточка #{card_id} обновлена.", reply_markup=DRAW_BUTTON)
            else:
                await update.message.reply_text(f"Не нашла карточку #{card_id} — возможно, её удалили.", reply_markup=DRAW_BUTTON)
        except Exception as e:
            logger.exception("Ошибка при редактировании карточки текстом")
            await update.message.reply_text(f"Не удалось обновить карточку: {e}", reply_markup=DRAW_BUTTON)
        return
    if not context.user_data.get("awaiting_text_card"):
        await start(update, context)
        return
    context.user_data["awaiting_text_card"] = False
    text = update.message.text
    if not text or not text.strip():
        return
    try:
        content_hash = compute_content_hash(text.strip().encode("utf-8"))
        dup = storage.find_duplicate(content_hash=content_hash)
        if dup:
            await _ask_duplicate_confirmation(
                context.bot, update.effective_chat.id, dup,
                {"is_text": True, "text": text, "content_hash": content_hash},
            )
            return
        pages = render_text_card_pages(text)
        file_ids = []
        for page_bytes in pages:
            sent = await send_photo_with_retry(update.message.reply_photo, page_bytes)
            file_ids.append(sent.photo[-1].file_id)
        if len(file_ids) == 1:
            new_id = storage.add_card(file_ids[0], kind="photo", content_hash=content_hash)
        else:
            new_id = storage.add_multi_card(file_ids, kind="photo", content_hash=content_hash)
        page_note = f" ({len(file_ids)} стр.)" if len(file_ids) > 1 else ""
        await update.message.reply_text(
            f"Добавлено из текста{page_note}! Карточка #{new_id}. Всего карточек: {storage.count()}.",
            reply_markup=DRAW_BUTTON,
        )
    except Exception as e:
        logger.exception("Ошибка при добавлении текстовой карточки")
        await update.message.reply_text(f"Не удалось добавить карточку из текста: {e}", reply_markup=DRAW_BUTTON)


async def admin_ignore_non_admin_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        return
    await update.message.reply_text("Фото принимает только администратор коллекции.")


async def count_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    ids = storage.list_ids()
    preview = ", ".join(str(i) for i in ids[:30])
    more = f" и ещё {len(ids) - 30}" if len(ids) > 30 else ""
    await update.effective_message.reply_text(f"Всего карточек: {storage.count()}.\nНомера: {preview}{more}")


async def _reply_with_card(message, card_id: int):
    """Отправляет карточку по номеру через переданный message (умеет reply_photo/reply_document)."""
    card = next((c for c in storage.cards if c["id"] == card_id), None)
    if card is None:
        await message.reply_text(f"Карточка #{card_id} не найдена.", reply_markup=DRAW_BUTTON)
        return
    caption = f"Карточка #{card_id} ({len(card_file_ids(card))} стр.)."
    file_ids = card_file_ids(card)
    for i, fid in enumerate(file_ids):
        is_last = i == len(file_ids) - 1
        if card.get("kind") == "document":
            await message.reply_document(document=fid, caption=caption if is_last else None, reply_markup=DRAW_BUTTON if is_last else None)
        else:
            await message.reply_photo(photo=fid, caption=caption if is_last else None, reply_markup=DRAW_BUTTON if is_last else None)


async def card_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Формат: /card <номер карточки>, например /card 3")
        return
    await _reply_with_card(update.message, int(context.args[0]))


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Формат: /delete <номер карточки>, например /delete 3")
        return
    await _prompt_delete_confirmation(update.message, int(context.args[0]))


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")


MAX_ZIP_BYTES = 40 * 1024 * 1024  # запас от лимита Telegram в 50 МБ на файл


def parse_card_selection(text: str, valid_ids: list) -> list:
    """Разбирает ввод вида '5,7,12-15' или 'все'/'all' в список существующих номеров карточек."""
    text = text.strip().lower()
    valid_set = set(valid_ids)
    if text in ("все", "всё", "all"):
        return sorted(valid_set)
    result = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a.isdigit() and b.isdigit():
                lo, hi = int(a), int(b)
                if lo > hi:
                    lo, hi = hi, lo
                result.update(range(lo, hi + 1))
        elif part.isdigit():
            result.add(int(part))
    return sorted(i for i in result if i in valid_set)


async def _export_cards(message, context: ContextTypes.DEFAULT_TYPE, cards: list):
    if not cards:
        await message.reply_text("Не нашла ни одной подходящей карточки.", reply_markup=DRAW_BUTTON)
        return
    await message.reply_text(f"Начинаю выгрузку {len(cards)} карточек, это может занять время...")

    buf = io.BytesIO()
    zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED)
    part = 1
    count_in_zip = 0
    failed = []

    for card in cards:
        file_ids = card_file_ids(card)
        for page_num, fid in enumerate(file_ids, start=1):
            try:
                tg_file = await context.bot.get_file(fid)
                data = await tg_file.download_as_bytearray()
            except Exception as e:
                logger.warning("Не удалось скачать карточку #%s (стр. %s): %s", card["id"], page_num, e)
                failed.append(card["id"])
                continue
            suffix = f"_p{page_num}" if len(file_ids) > 1 else ""
            zf.writestr(f"card_{card['id']:04d}{suffix}.jpg", bytes(data))
            count_in_zip += 1
            if buf.tell() > MAX_ZIP_BYTES:
                zf.close()
                buf.seek(0)
                await message.reply_document(document=buf, filename=f"cards_part{part}.zip")
                part += 1
                buf = io.BytesIO()
                zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED)
                count_in_zip = 0

    zf.close()
    if count_in_zip > 0:
        buf.seek(0)
        await message.reply_document(document=buf, filename=f"cards_part{part}.zip")

    msg = "Готово! Карточки отправлены архивом(-ами)."
    if failed:
        msg += f"\nНе удалось скачать: {failed}"
    await message.reply_text(msg, reply_markup=DRAW_BUTTON)


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await _export_cards(update.effective_message, context, list(storage.cards))


def schedule_reminder(application: Application, user_id: int, hour: int, minute: int):
    for job in application.job_queue.get_jobs_by_name(str(user_id)):
        job.schedule_removal()
    application.job_queue.run_daily(
        send_daily_card,
        time=datetime.time(hour=hour, minute=minute, tzinfo=datetime.timezone.utc),
        chat_id=user_id,
        name=str(user_id),
    )


async def send_daily_card(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    card = storage.next_card_for_user(chat_id)
    if card is None:
        return
    record_view(chat_id)
    kb = _fav_keyboard(card["id"])
    file_ids = card_file_ids(card)
    for i, fid in enumerate(file_ids):
        is_last = i == len(file_ids) - 1
        cap = "🌅 Карточка дня" if is_last else None
        if card.get("kind") == "document":
            await context.bot.send_document(chat_id=chat_id, document=fid, caption=cap, reply_markup=kb if is_last else None)
        else:
            await context.bot.send_photo(chat_id=chat_id, photo=fid, caption=cap, reply_markup=kb if is_last else None)


TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


REMINDER_TIME_OPTIONS = ["08:00", "10:00", "12:00", "18:00", "21:00"]


def _reminder_keyboard(user_id: int) -> InlineKeyboardMarkup:
    current = reminders.all().get(str(user_id))
    row = []
    for t in REMINDER_TIME_OPTIONS:
        hour, minute = map(int, t.split(":"))
        msk = f"{(hour + 3) % 24:02d}:{minute:02d} МСК"
        label = f"✅ {msk}" if t == current else msk
        row.append(InlineKeyboardButton(label, callback_data=f"remind_set:{t}"))
    rows = [row[:2], row[2:4], row[4:]]
    rows.append([InlineKeyboardButton("❌ Выключить напоминание", callback_data="remind_off")])
    return InlineKeyboardMarkup(rows)


async def reminder_menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = reminders.all().get(str(update.effective_user.id))
    text = "Выбери время ежедневной карточки:"
    if current:
        hour, minute = map(int, current.split(":"))
        text += f"\n\nСейчас включено на {(hour + 3) % 24:02d}:{minute:02d} МСК."
    await update.message.reply_text(text, reply_markup=_reminder_keyboard(update.effective_user.id))


async def reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "remind_off":
        ok = reminders.remove(update.effective_user.id)
        for job in context.application.job_queue.get_jobs_by_name(str(update.effective_user.id)):
            job.schedule_removal()
        await query.answer("Напоминание выключено" if ok else "Напоминание и так было выключено")
        await query.edit_message_text(
            "Напоминание выключено.", reply_markup=_reminder_keyboard(update.effective_user.id)
        )
        return

    _, time_str = query.data.split(":", 1)
    hour, minute = map(int, time_str.split(":"))
    reminders.set(update.effective_user.id, time_str)
    schedule_reminder(context.application, update.effective_user.id, hour, minute)
    await query.answer(f"Включено на {(hour + 3) % 24:02d}:{minute:02d} МСК")
    await query.edit_message_text(
        f"Готово! Буду присылать карточку каждый день в {(hour + 3) % 24:02d}:{minute:02d} МСК.",
        reply_markup=_reminder_keyboard(update.effective_user.id),
    )


async def remind_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Формат: /remind_on 09:00 (время по UTC).")
        return
    m = TIME_RE.match(context.args[0])
    if not m:
        await update.message.reply_text("Не понял время. Формат ЧЧ:ММ, например /remind_on 09:00 (UTC).")
        return
    hour, minute = int(m.group(1)), int(m.group(2))
    reminders.set(update.effective_user.id, f"{hour:02d}:{minute:02d}")
    schedule_reminder(context.application, update.effective_user.id, hour, minute)
    await update.message.reply_text(
        f"Готово! Буду присылать карточку каждый день в {hour:02d}:{minute:02d} по UTC "
        f"(по Москве это ~{(hour + 3) % 24:02d}:{minute:02d} МСК)."
    )


async def remind_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok = reminders.remove(update.effective_user.id)
    for job in context.application.job_queue.get_jobs_by_name(str(update.effective_user.id)):
        job.schedule_removal()
    await update.message.reply_text("Напоминания отключены." if ok else "У тебя не было включённых напоминаний.")


async def health(request):
    return web.Response(text="OK")


async def self_ping():
    if not EXTERNAL_URL:
        logger.warning("EXTERNAL_URL не задан — автопинг выключен, бот может засыпать")
        return
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(600)  # 10 минут
            try:
                async with session.get(EXTERNAL_URL, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    logger.info("self-ping %s -> %s", EXTERNAL_URL, r.status)
            except Exception as e:
                logger.warning("self-ping failed: %s", e)


async def main():
    application = Application.builder().token(BOT_TOKEN).concurrent_updates(8).build()
    application.add_handler(MessageHandler(filters.ALL, track_user), group=-1)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("count", count_cmd))
    application.add_handler(CommandHandler("card", card_cmd))
    application.add_handler(CommandHandler("delete", delete_cmd))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(CommandHandler("export", export_cmd))
    application.add_handler(CommandHandler("reprocess", reprocess_cmd))
    application.add_handler(CommandHandler("import_cards", import_cards_cmd))
    application.add_handler(CommandHandler("hash_missing", hash_missing_cmd))
    application.add_handler(CommandHandler("favorites", favorites_cmd))
    application.add_handler(CommandHandler("remind_on", remind_on_cmd))
    application.add_handler(CommandHandler("remind_off", remind_off_cmd))
    application.add_handler(CallbackQueryHandler(favorite_callback, pattern="^fav:"))
    application.add_handler(CallbackQueryHandler(mode_choice_callback, pattern="^mode:"))
    application.add_handler(CallbackQueryHandler(unfavorite_callback, pattern="^unfav:"))
    application.add_handler(CallbackQueryHandler(reminder_callback, pattern="^remind_"))
    application.add_handler(CallbackQueryHandler(admin_menu_callback, pattern="^menu_"))
    application.add_handler(CallbackQueryHandler(delete_confirm_callback, pattern="^(confirmdel:|canceldel$)"))
    application.add_handler(CallbackQueryHandler(duplicate_confirm_callback, pattern="^(forceadd:|skipadd:)"))
    application.add_handler(CallbackQueryHandler(group_choice_callback, pattern="^(splitgroup:|combinegroup:)"))
    application.add_handler(MessageHandler(filters.Regex("^💎 Открыть жемчужину души$"), draw_card))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(MODE_BUTTON_TEXT)}$"), mode_prompt))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(FAVORITES_BUTTON_TEXT)}$"), favorites_cmd))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(STATS_BUTTON_TEXT)}$"), stats_cmd))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(REMINDER_BUTTON_TEXT)}$"), reminder_menu_cmd))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(TEXT_CARD_BUTTON_TEXT)}$"), text_card_prompt))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(ADMIN_MENU_BUTTON_TEXT)}$"), admin_menu_prompt))
    application.add_handler(MessageHandler(filters.PHOTO & filters.User(ADMIN_ID), admin_add_card_photo))
    application.add_handler(MessageHandler(filters.Document.PDF & filters.User(ADMIN_ID), admin_add_card_pdf))
    application.add_handler(MessageHandler(filters.Document.IMAGE & filters.User(ADMIN_ID), admin_add_card_document))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.User(ADMIN_ID), admin_ignore_non_admin_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), admin_add_card_text))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, start))

    for uid_str, time_str in reminders.all().items():
        hour, minute = map(int, time_str.split(":"))
        schedule_reminder(application, int(uid_str), hour, minute)

    web_app = web.Application()
    web_app.router.add_get("/", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Веб-сервер запущен на порту %s", PORT)

    asyncio.create_task(self_ping())

    async with application:
        await application.start()
        if meta.get("keyboard_version") != KEYBOARD_VERSION:
            asyncio.create_task(broadcast_keyboard_update(application))
            meta.set("keyboard_version", KEYBOARD_VERSION)
        await application.updater.start_polling(drop_pending_updates=True)
        logger.info("Бот запущен, ждём сообщений...")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
