import json
import random
import logging

import imagehash
from github import Github, Auth
from github.GithubException import GithubException

logger = logging.getLogger(__name__)

# Все коммиты бота (сохранение карточек/избранного/напоминаний/пользователей/меты) уходят
# в отдельную ветку DATA_BRANCH, а не в main. Render настроен на автодеплой при пуше в main,
# и раньше каждое сохранение карточки перезапускало весь бот-процесс (убивая по дороге буфер
# альбома в памяти). Ветку код-репозитория (main) теперь трогают только настоящие правки кода.
DATA_BRANCH = "data"


def card_file_ids(card: dict) -> list:
    """Список file_id карточки — поддерживает и старый формат (один file_id),
    и новый многостраничный (file_ids: список)."""
    if "file_ids" in card:
        return card["file_ids"]
    return [card["file_id"]]


class CardStorage:
    """Хранит карточки (file_id фото + текст) в cards.json в GitHub-репозитории.

    SQLite на бесплатном Render не подходит: диск стирается при каждом
    рестарте/редеплое сервиса. GitHub-репозиторий переживает это без проблем.
    """

    def __init__(self, github_token: str, repo_name: str, file_path: str = "cards.json"):
        self.repo = Github(auth=Auth.Token(github_token)).get_user().get_repo(repo_name)
        self.file_path = file_path
        self.cards = []
        self._sha = None
        self._decks = {}
        self._modes = {}
        self._seq_positions = {}
        self._load()

    def _load(self):
        try:
            f = self.repo.get_contents(self.file_path, ref=DATA_BRANCH)
            data = json.loads(f.decoded_content.decode())
            self.cards = data.get("cards", [])
            self._sha = f.sha
            logger.info("Загружено карточек: %d", len(self.cards))
        except Exception as e:
            logger.warning("cards.json не найден, стартуем с пустого списка: %s", e)
            self.cards = []
            self._sha = None

    def _save(self, commit_message: str):
        content = json.dumps({"cards": self.cards}, ensure_ascii=False, indent=2)
        if self._sha:
            result = self.repo.update_file(self.file_path, commit_message, content, self._sha, branch=DATA_BRANCH)
        else:
            result = self.repo.create_file(self.file_path, commit_message, content, branch=DATA_BRANCH)
        self._sha = result["content"].sha

    def persist(self, commit_message: str):
        """Сохранить текущее состояние self.cards (например, после ручного изменения file_id/kind)."""
        self._save(commit_message)

    def add_card(self, file_id: str, kind: str = "photo", phash: str | None = None, content_hash: str | None = None, max_attempts: int = 5) -> int:
        for attempt in range(max_attempts):
            new_id = max((c["id"] for c in self.cards), default=0) + 1
            card = {"id": new_id, "file_id": file_id, "kind": kind}
            if phash:
                card["phash"] = phash
            if content_hash:
                card["content_hash"] = content_hash
            self.cards.append(card)
            try:
                self._save(f"add card #{new_id}")
                return new_id
            except GithubException as e:
                self.cards.pop()
                if getattr(e, "status", None) == 409 and attempt < max_attempts - 1:
                    logger.warning("Конфликт версии cards.json (add_card), перечитываю и повторяю: попытка %s", attempt + 1)
                    self._load()
                    continue
                raise

    def add_multi_card(self, file_ids: list, kind: str = "photo", phash: str | None = None, content_hash: str | None = None, max_attempts: int = 5) -> int:
        """Карточка из нескольких страниц (напр. многостраничный PDF) — при показе
        все страницы отправляются одна за другой, это одна карточка в колоде."""
        for attempt in range(max_attempts):
            new_id = max((c["id"] for c in self.cards), default=0) + 1
            card = {"id": new_id, "file_ids": list(file_ids), "kind": kind}
            if phash:
                card["phash"] = phash
            if content_hash:
                card["content_hash"] = content_hash
            self.cards.append(card)
            try:
                self._save(f"add multi-page card #{new_id} ({len(file_ids)} pages)")
                return new_id
            except GithubException as e:
                self.cards.pop()
                if getattr(e, "status", None) == 409 and attempt < max_attempts - 1:
                    logger.warning("Конфликт версии cards.json (add_multi_card), перечитываю и повторяю: попытка %s", attempt + 1)
                    self._load()
                    continue
                raise

    def update_card(
        self,
        card_id: int,
        file_id: str | None = None,
        file_ids: list | None = None,
        kind: str | None = None,
        phash: str | None = None,
        content_hash: str | None = None,
        max_attempts: int = 5,
    ) -> bool:
        """Заменяет содержимое существующей карточки (номер/позиция в колоде сохраняются)."""
        for attempt in range(max_attempts):
            idx = next((i for i, c in enumerate(self.cards) if c["id"] == card_id), None)
            if idx is None:
                return False
            original = dict(self.cards[idx])
            card = self.cards[idx]
            if file_id is not None:
                card.pop("file_ids", None)
                card["file_id"] = file_id
            if file_ids is not None:
                card.pop("file_id", None)
                card["file_ids"] = list(file_ids)
            if kind is not None:
                card["kind"] = kind
            if phash is not None:
                card["phash"] = phash
            if content_hash is not None:
                card["content_hash"] = content_hash
            try:
                self._save(f"edit card #{card_id}")
                return True
            except GithubException as e:
                self.cards[idx] = original
                if getattr(e, "status", None) == 409 and attempt < max_attempts - 1:
                    logger.warning("Конфликт версии cards.json (update_card), перечитываю и повторяю: попытка %s", attempt + 1)
                    self._load()
                    continue
                raise
        return False

    def find_duplicate(self, phash: str | None = None, content_hash: str | None = None, max_distance: int = 0):
        """Дубликатом считается только карточка с ТЕМ ЖЕ точным содержимым файла (content_hash).
        Перцептивный хэш (phash) больше не используется для решения — визуально похожие,
        но разные по содержанию карточки (частая история при едином стиле дизайна колоды)
        из-за него ошибочно принимались за дубли."""
        if content_hash:
            for c in self.cards:
                if c.get("content_hash") == content_hash:
                    return c
        return None

    def random_card(self):
        if not self.cards:
            return None
        return random.choice(self.cards)

    def next_card_for_user(self, user_id: int):
        """Тянет карточку без повторов, пока не покажет все — потом тасует заново."""
        if not self.cards:
            return None
        deck = self._decks.get(user_id)
        if not deck:
            deck = [c["id"] for c in self.cards]
            random.shuffle(deck)
            self._decks[user_id] = deck
        card_id = deck.pop()
        card = next((c for c in self.cards if c["id"] == card_id), None)
        if card is None:
            # карточку успели удалить между тасовками — тянем следующую
            return self.next_card_for_user(user_id)
        return card

    def set_mode(self, user_id: int, mode: str):
        """mode: 'random' или 'sequential'."""
        self._modes[user_id] = mode

    def get_mode(self, user_id: int) -> str:
        return self._modes.get(user_id, "random")

    def reset_sequential(self, user_id: int):
        self._seq_positions[user_id] = 0

    def next_sequential_card(self, user_id: int):
        """Показывает карточки по порядку номеров; продолжает с того места, где остановились."""
        if not self.cards:
            return None
        sorted_ids = sorted(c["id"] for c in self.cards)
        pos = self._seq_positions.get(user_id, 0)
        if pos >= len(sorted_ids):
            pos = 0
        card_id = sorted_ids[pos]
        self._seq_positions[user_id] = pos + 1
        card = next((c for c in self.cards if c["id"] == card_id), None)
        if card is None:
            return self.next_sequential_card(user_id)
        return card

    def delete_card(self, card_id: int, max_attempts: int = 5) -> tuple:
        """Удаляет карточку и перенумеровывает оставшиеся без дыр (в одном коммите).
        Возвращает (удалена_ли, mapping) — mapping {старый_id: новый_id} для карточек,
        чей номер сдвинулся (нужно применить к FavoritesStore вызывающей стороне)."""
        deleted, mapping = self.delete_cards([card_id], max_attempts=max_attempts)
        return (card_id in deleted), mapping

    def delete_cards(self, card_ids: list, max_attempts: int = 5) -> tuple:
        """Удаляет сразу несколько карточек и перенумеровывает оставшиеся без дыр
        в ОДНОМ коммите. Возвращает (список реально удалённых id, mapping
        {старый_id: новый_id} для карточек, чей номер сдвинулся)."""
        id_set = set(card_ids)
        for attempt in range(max_attempts):
            before = list(self.cards)
            existing_ids = {c["id"] for c in self.cards}
            deleted = sorted(id_set & existing_ids)
            if not deleted:
                return [], {}
            self.cards = [c for c in self.cards if c["id"] not in id_set]
            mapping = self._renumber()
            try:
                self._save(f"delete cards {deleted}, renumber {len(mapping)} shifted")
                return deleted, mapping
            except GithubException as e:
                self.cards = before
                if getattr(e, "status", None) == 409 and attempt < max_attempts - 1:
                    logger.warning("Конфликт версии cards.json (delete_cards), перечитываю и повторяю: попытка %s", attempt + 1)
                    self._load()
                    continue
                raise
        return [], {}

    def _renumber(self) -> dict:
        """Сортирует карточки по текущему id и присваивает новые id 1..N без дыр.
        Возвращает mapping {старый_id: новый_id} только для карточек, чей номер
        реально изменился (пустой словарь, если дыр не было)."""
        self.cards.sort(key=lambda c: c["id"])
        mapping = {}
        for i, card in enumerate(self.cards, start=1):
            old_id = card["id"]
            if old_id != i:
                mapping[old_id] = i
                card["id"] = i
        return mapping

    def list_ids(self) -> list:
        return [c["id"] for c in self.cards]

    def count(self) -> int:
        return len(self.cards)


class FavoritesStore:
    """Избранные карточки пользователей — favorites.json в GitHub."""

    def __init__(self, github_token: str, repo_name: str, file_path: str = "favorites.json"):
        self.repo = Github(auth=Auth.Token(github_token)).get_user().get_repo(repo_name)
        self.file_path = file_path
        self.data = {}  # str(user_id) -> [card_id, ...]
        self._sha = None
        self._load()

    def _load(self):
        try:
            f = self.repo.get_contents(self.file_path, ref=DATA_BRANCH)
            self.data = json.loads(f.decoded_content.decode())
            self._sha = f.sha
        except Exception as e:
            logger.warning("favorites.json не найден, стартуем с пустого: %s", e)
            self.data = {}
            self._sha = None

    def _save(self, commit_message: str):
        content = json.dumps(self.data, ensure_ascii=False, indent=2)
        if self._sha:
            result = self.repo.update_file(self.file_path, commit_message, content, self._sha, branch=DATA_BRANCH)
        else:
            result = self.repo.create_file(self.file_path, commit_message, content, branch=DATA_BRANCH)
        self._sha = result["content"].sha

    def add(self, user_id: int, card_id: int) -> bool:
        key = str(user_id)
        favs = self.data.setdefault(key, [])
        if card_id in favs:
            return False
        favs.append(card_id)
        self._save(f"favorite add user={user_id} card={card_id}")
        return True

    def list_for_user(self, user_id: int) -> list:
        return self.data.get(str(user_id), [])

    def remove(self, user_id: int, card_id: int) -> bool:
        key = str(user_id)
        favs = self.data.get(key, [])
        if card_id not in favs:
            return False
        favs.remove(card_id)
        self.data[key] = favs
        self._save(f"favorite remove user={user_id} card={card_id}")
        return True

    def remap_ids(self, mapping: dict):
        """Применяет сдвиг номеров карточек (после удаления + перенумерации) ко всем
        избранным — так карточка остаётся в избранном у пользователя, просто под новым
        номером, а не теряется."""
        if not mapping:
            return
        changed = False
        for uid, ids in self.data.items():
            new_ids = [mapping.get(i, i) for i in ids]
            if new_ids != ids:
                self.data[uid] = new_ids
                changed = True
        if changed:
            self._save(f"remap {len(mapping)} favorite ids after renumber")


class ReminderStore:
    """Кто и в какое время (UTC) хочет получать карточку дня — reminders.json в GitHub."""

    def __init__(self, github_token: str, repo_name: str, file_path: str = "reminders.json"):
        self.repo = Github(auth=Auth.Token(github_token)).get_user().get_repo(repo_name)
        self.file_path = file_path
        self.data = {}  # str(user_id) -> "HH:MM"
        self._sha = None
        self._load()

    def _load(self):
        try:
            f = self.repo.get_contents(self.file_path, ref=DATA_BRANCH)
            self.data = json.loads(f.decoded_content.decode())
            self._sha = f.sha
        except Exception as e:
            logger.warning("reminders.json не найден, стартуем с пустого: %s", e)
            self.data = {}
            self._sha = None

    def _save(self, commit_message: str):
        content = json.dumps(self.data, ensure_ascii=False, indent=2)
        if self._sha:
            result = self.repo.update_file(self.file_path, commit_message, content, self._sha, branch=DATA_BRANCH)
        else:
            result = self.repo.create_file(self.file_path, commit_message, content, branch=DATA_BRANCH)
        self._sha = result["content"].sha

    def set(self, user_id: int, time_str: str):
        self.data[str(user_id)] = time_str
        self._save(f"reminder set user={user_id} time={time_str}")

    def remove(self, user_id: int) -> bool:
        key = str(user_id)
        if key not in self.data:
            return False
        del self.data[key]
        self._save(f"reminder remove user={user_id}")
        return True

    def all(self) -> dict:
        return dict(self.data)


class UserStore:
    """Все, кто хоть раз писал боту — users.json в GitHub. Нужно для рассылок-уведомлений."""

    def __init__(self, github_token: str, repo_name: str, file_path: str = "users.json"):
        self.repo = Github(auth=Auth.Token(github_token)).get_user().get_repo(repo_name)
        self.file_path = file_path
        self.data = []  # список user_id
        self._sha = None
        self._load()

    def _load(self):
        try:
            f = self.repo.get_contents(self.file_path, ref=DATA_BRANCH)
            self.data = json.loads(f.decoded_content.decode())
            self._sha = f.sha
        except Exception as e:
            logger.warning("users.json не найден, стартуем с пустого: %s", e)
            self.data = []
            self._sha = None

    def _save(self, commit_message: str):
        content = json.dumps(self.data, ensure_ascii=False, indent=2)
        if self._sha:
            result = self.repo.update_file(self.file_path, commit_message, content, self._sha, branch=DATA_BRANCH)
        else:
            result = self.repo.create_file(self.file_path, commit_message, content, branch=DATA_BRANCH)
        self._sha = result["content"].sha

    def add(self, user_id: int) -> bool:
        if user_id in self.data:
            return False
        self.data.append(user_id)
        self._save(f"track new user {user_id}")
        return True

    def all(self) -> list:
        return list(self.data)


class MetaStore:
    """Служебные флаги (напр. версия клавиатуры) — meta.json в GitHub."""

    def __init__(self, github_token: str, repo_name: str, file_path: str = "meta.json"):
        self.repo = Github(auth=Auth.Token(github_token)).get_user().get_repo(repo_name)
        self.file_path = file_path
        self.data = {}
        self._sha = None
        self._load()

    def _load(self):
        try:
            f = self.repo.get_contents(self.file_path, ref=DATA_BRANCH)
            self.data = json.loads(f.decoded_content.decode())
            self._sha = f.sha
        except Exception as e:
            logger.warning("meta.json не найден, стартуем с пустого: %s", e)
            self.data = {}
            self._sha = None

    def _save(self, commit_message: str):
        content = json.dumps(self.data, ensure_ascii=False, indent=2)
        if self._sha:
            result = self.repo.update_file(self.file_path, commit_message, content, self._sha, branch=DATA_BRANCH)
        else:
            result = self.repo.create_file(self.file_path, commit_message, content, branch=DATA_BRANCH)
        self._sha = result["content"].sha

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value):
        self.data[key] = value
        self._save(f"meta set {key}={value}")
