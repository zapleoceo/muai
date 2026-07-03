from vera_shared.tools.spec import ToolParam, ToolSpec

TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="telegram_list_recent_dialogs",
        description=(
            "List user's most recently active Telegram dialogs, sorted by last "
            "message date (newest first). Use this when the user asks about "
            "'active chats', 'recent chats', 'where I talked lately', without "
            "naming a specific peer. Returns id, name, type, unread_count, "
            "last_message_date for each."
        ),
        params=[
            ToolParam("limit", "integer", "How many dialogs to return.",
                      required=False, default=15),
            ToolParam("exclude_channels", "boolean",
                      "Skip broadcast channels (keep only people, groups, supergroups).",
                      required=False, default=False),
            ToolParam("only_unread", "boolean",
                      "Return only dialogs with unread messages.",
                      required=False, default=False),
        ],
    ),
    ToolSpec(
        name="telegram_search_public",
        description=(
            "GLOBAL Telegram search for public groups, channels and users — "
            "including ones the user has NOT joined yet. Use for 'find public "
            "groups about X', 'are there channels on Y'. Returns id, name, "
            "type, username, t.me link, participants count. Differs from "
            "telegram_search_dialogs, which only searches the user's OWN chats."
        ),
        params=[
            ToolParam("query", "string", "Search string (title/username)."),
            ToolParam("limit", "integer", "Max results.", required=False, default=20),
        ],
    ),
    ToolSpec(
        name="telegram_search_dialogs",
        description=(
            "Find Telegram chats/channels/users by name OR by folder "
            "membership. Returns candidates with id, name, type, folders, "
            "and 'match' field showing whether matched by title or folder. "
            "If nothing matches, the result includes a _note with all known "
            "folder titles — use telegram_list_folders for the full view. "
            "Searches Cyrillic + Latin transliterations + loose spacing."
        ),
        params=[
            ToolParam("query", "string", "Search string (case-insensitive substring on dialog names)."),
            ToolParam("limit", "integer", "Max results.", required=False, default=15),
        ],
    ),
    ToolSpec(
        name="telegram_read_messages",
        description=(
            "Read recent messages from a Telegram chat. Pass chat_id (preferred, "
            "from search_dialogs) OR peer (string name). Returns each message "
            "with date, text, from, out, has_image, has_ocr. When a message "
            "contains a photo or image-document and ocr_images=true (default), "
            "the image is downloaded and OCR'd via Gemini — recognized text is "
            "appended to message.text as '[OCR]:\\n…'. So screenshots of "
            "balances, tables, receipts become readable. Bounded to 6 OCR "
            "calls per request."
        ),
        params=[
            ToolParam("chat_id", "integer", "Telegram entity id from search_dialogs. Preferred over peer.",
                      required=False, default=0),
            ToolParam("peer", "string", "Chat name as fallback when chat_id unknown.",
                      required=False, default=""),
            ToolParam("days", "integer", "How many days back to read.",
                      required=False, default=1),
            ToolParam("limit", "integer", "Max messages to fetch.",
                      required=False, default=50),
            ToolParam("ocr_images", "boolean", "Run OCR on image attachments.",
                      required=False, default=True),
        ],
    ),
    ToolSpec(
        name="telegram_send_message",
        description="Send a Telegram message. Pass chat_id (preferred) or peer.",
        params=[
            ToolParam("chat_id", "integer", "Target chat id.", required=False, default=0),
            ToolParam("peer", "string", "Target chat name.", required=False, default=""),
            ToolParam("text", "string", "Message body."),
        ],
    ),
    ToolSpec(
        name="telegram_get_dialog_info",
        description="Get info about a Telegram chat: name, type, participants count.",
        params=[
            ToolParam("chat_id", "integer", "Target chat id.", required=False, default=0),
            ToolParam("peer", "string", "Target chat name.", required=False, default=""),
        ],
    ),
    ToolSpec(
        name="telegram_delete_messages",
        description=(
            "Delete specific Telegram messages by id. Own messages always "
            "deletable; others require admin with delete_messages right. "
            "revoke=True deletes from both sides; False — only from your view."
        ),
        params=[
            ToolParam("chat_id", "integer", "Target chat id.", required=False, default=0),
            ToolParam("peer", "string", "Target chat name.", required=False, default=""),
            ToolParam("message_ids", "array",
                      "List of message ids to delete (integers)."),
            ToolParam("revoke", "boolean",
                      "Delete for both sides (default true).",
                      required=False, default=True),
        ],
    ),
    ToolSpec(
        name="telegram_folder_digest",
        description=(
            "ИДЕАЛЬНЫЙ инструмент для запросов «что в папке X сегодня», "
            "«саммари по группе чатов», «о чём говорили в Work за неделю». "
            "Map-reduce: читает ВСЕ чаты папки полностью (без потери "
            "контекста), для каждого активного чата делает отдельный "
            "LLM-вызов для краткого саммари (1-3 строки), возвращает "
            "агрегированный dict {folder, chats_total, active:[{chat,"
            "summary,...}], silent_chats:[...]}. Используй ВМЕСТО ручной "
            "цепочки list_folders → read_messages_batch когда нужен "
            "именно саммари по папке."
        ),
        params=[
            ToolParam("folder_title", "string",
                      "Название папки (case-insensitive, loose match)."),
            ToolParam("days", "integer", "Окно в днях.",
                      required=False, default=1),
            ToolParam("limit_per_chat", "integer",
                      "Максимум сообщений на чат.",
                      required=False, default=50),
        ],
    ),
    ToolSpec(
        name="telegram_read_messages_batch",
        description=(
            "Read messages from MULTIPLE chats in one call — for folder "
            "digests like «что обсудили в папке ItStep сегодня». MUCH "
            "better than N separate telegram_read_messages calls. After "
            "telegram_list_folders, pass the entire peer_ids array here. "
            "Returns aggregated dict with chats_total, chats_with_messages, "
            "and per-chat results."
        ),
        params=[
            ToolParam("chat_ids", "array", "List of chat ids (integers)."),
            ToolParam("days", "integer", "Lookback window in days.",
                      required=False, default=1),
            ToolParam("limit_per_chat", "integer",
                      "Max messages per chat.", required=False, default=30),
            ToolParam("ocr_images", "boolean",
                      "Run OCR on images (slow).", required=False, default=False),
        ],
    ),
    ToolSpec(
        name="telegram_list_folders",
        description=(
            "List all Telegram folders (Dialog Filters) with their titles "
            "and chat counts. Use BEFORE telegram_search_dialogs when the "
            "user asks about a group/category that might be a folder name "
            "(e.g. 'все чаты в группу ItStep' — first list_folders, then "
            "use the matched folder's peer_ids)."
        ),
        params=[],
    ),
    ToolSpec(
        name="telegram_list_forum_topics",
        description=(
            "List all forum topics (threads) in a supergroup with their "
            "ids and titles. Use this BEFORE bot_delete_forum_topic when "
            "the user asks to clear all topics — you need the ids first."
        ),
        params=[
            ToolParam("chat_id", "integer", "Target supergroup id.", required=False, default=0),
            ToolParam("peer", "string", "Group name as fallback.", required=False, default=""),
            ToolParam("limit", "integer", "Max topics.", required=False, default=100),
        ],
    ),
    ToolSpec(
        name="telegram_clear_history",
        description=(
            "Wipe entire chat history with a peer. Use carefully — "
            "destructive. just_clear=True hides from your side only. "
            "revoke=True attempts both-side deletion (works in personal "
            "chats; in groups needs admin)."
        ),
        params=[
            ToolParam("chat_id", "integer", "Target chat id.", required=False, default=0),
            ToolParam("peer", "string", "Target chat name.", required=False, default=""),
            ToolParam("just_clear", "boolean",
                      "Hide from your side only (safe).",
                      required=False, default=False),
            ToolParam("revoke", "boolean",
                      "Delete for both sides (irreversible).",
                      required=False, default=False),
            ToolParam("max_id", "integer",
                      "Delete up to this msg id (0 = all).",
                      required=False, default=0),
        ],
    ),
]
