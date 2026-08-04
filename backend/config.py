import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    APP_NAME: str = "Ruijie Router Monitor"
    
    # Router Credentials & Address
    RUIJIE_HOST: str = os.getenv("RUIJIE_HOST", "http://192.168.8.1")
    RUIJIE_USER: str = os.getenv("RUIJIE_USER", "admin")
    RUIJIE_PASS: str = os.getenv("RUIJIE_PASS", "")
    
    POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "3"))  # seconds
    
    # Telegram Notifications
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_ENABLE: bool = os.getenv("TELEGRAM_ENABLE", "false").lower() == "true"

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./data/monitor.db")

    def save(self):
        import tempfile
        def _quote_env(value: str) -> str:
            escaped = value.replace('\\', '\\\\').replace('"', '\\"')
            return f'"{escaped}"'

        env_file = os.path.join(os.path.dirname(__file__), ".env")
        env_dir = os.path.dirname(env_file)
        
        values = {
            "RUIJIE_HOST": self.RUIJIE_HOST,
            "RUIJIE_USER": self.RUIJIE_USER,
            "RUIJIE_PASS": self.RUIJIE_PASS,
            "POLL_INTERVAL": str(self.POLL_INTERVAL),
            "TELEGRAM_BOT_TOKEN": self.TELEGRAM_BOT_TOKEN,
            "TELEGRAM_CHAT_ID": self.TELEGRAM_CHAT_ID,
            "TELEGRAM_ENABLE": str(self.TELEGRAM_ENABLE).lower(),
            "DATABASE_URL": self.DATABASE_URL,
        }
        
        fd, temp_path = tempfile.mkstemp(prefix=".env.", dir=env_dir, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for k, v in values.items():
                    f.write(f"{k}={_quote_env(str(v))}\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, env_file)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

settings = Settings()
