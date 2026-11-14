-- Initializes the dedicated unprivileged database user for running student queries
CREATE USER eval_runner WITH PASSWORD 'eval_password';
GRANT ALL PRIVILEGES ON DATABASE neondb TO eval_runner;
ALTER DATABASE neondb OWNER TO eval_runner;
