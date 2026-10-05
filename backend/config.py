from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    supabase_url:           str
    supabase_anon_key:      str
    supabase_service_role:  str
    supabase_db_url:        str
    jwt_secret:             str = ""  # derived from Supabase JWT secret
    environment:            str = "development"

    class Config:
        env_file = "../JIM Deluxe/jim.env"
        env_file_encoding = "utf-8"
        extra = "ignore"

settings = Settings()
