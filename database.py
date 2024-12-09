import sqlite3
import datetime

class UserDatabase:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.init_db()

    def init_db(self):
        """Initialize the database schema."""
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    last_used DATE,
                    translations_today INTEGER DEFAULT 0,
                    is_premium BOOLEAN DEFAULT 0
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    username TEXT,
                    video_url TEXT,
                    translation_mode TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def add_user(self, user_id, username):
        """Add a user if they don't already exist."""
        with self.conn:
            self.conn.execute("""
                INSERT OR IGNORE INTO users (user_id, username, last_used, translations_today, is_premium)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, username, None, 0, False))

    def log_translation(self, user_id, username, video_url, translation_mode):
        """Log a translation attempt."""
        with self.conn:
            self.conn.execute("""
                INSERT INTO history (user_id, username, video_url, translation_mode)
                VALUES (?, ?, ?, ?)
            """, (user_id, username, video_url, translation_mode))

    def get_user_stats(self, user_id):
        """Retrieve user stats."""
        with self.conn:
            total_translations = self.conn.execute("""
                SELECT COUNT(*) FROM history WHERE user_id = ?
            """, (user_id,)).fetchone()[0]
            is_premium = self.conn.execute("""
                SELECT is_premium FROM users WHERE user_id = ?
            """, (user_id,)).fetchone()[0]
        return {"total_translations": total_translations, "is_premium": is_premium}


    def can_translate(self, user_id):
        """Check if the user can translate a video today."""
        with self.conn:
            user = self.conn.execute("""
                SELECT last_used, translations_today, is_premium FROM users WHERE user_id = ?
            """, (user_id,)).fetchone()

        today = datetime.date.today()
        if not user:
            return False  # User not found

        if user[2]:  # Premium users have no daily limit
            return True

        if user[0] == today.isoformat():
            return user[1] < 1  # Free users: 1 translation/day
        return True

    def log_successful_translation(self, user_id):
        """Log a successful translation."""
        today = datetime.date.today().isoformat()
        with self.conn:
            self.conn.execute("""
                UPDATE users
                SET last_used = ?, translations_today = translations_today + 1
                WHERE user_id = ?
            """, (today, user_id))

    def is_premium(self, user_id):
        """
        Check if a user is a premium user.

        Args:
            user_id (int): The Telegram user ID.

        Returns:
            bool: True if the user is premium, False otherwise.
        """
        query = "SELECT is_premium FROM users WHERE user_id = %s"
        with self.connection.cursor() as cursor:
            cursor.execute(query, (user_id,))
            result = cursor.fetchone()
            return result[0] if result else False
