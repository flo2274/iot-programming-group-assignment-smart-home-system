# database_utils.py

import os
import pymysql
from dotenv import load_dotenv
import datetime

load_dotenv()

DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_USER = os.getenv('DB_USER', 'db_user')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'db_password')
DB_NAME = os.getenv('DB_NAME', 'iot_smart_home')
DB_PORT = int(os.getenv('DB_PORT', 3306))

CREATE_SENSOR_DATA_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `sensor_data` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `temperature` FLOAT,
    `humidity` FLOAT,
    `light_level` INT,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;
"""
CREATE_TIMESTAMP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS `idx_created_at` ON `sensor_data` (`created_at`);
"""

def get_db_connection():
    try:
        connection = pymysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME, # Connect to the specific database
            port=DB_PORT,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10
        )
        return connection
    except pymysql.MySQLError as e:
        print(f"Error connecting to MySQL Database '{DB_NAME}': {e}")
        return None

def ensure_database_exists():
    try:
        connection_to_server = pymysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            port=DB_PORT,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10
        )
        with connection_to_server.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}`")
        connection_to_server.commit()
        print(f"Database '{DB_NAME}' ensured to exist.")
        return True
    except pymysql.MySQLError as e:
        print(f"Error ensuring database '{DB_NAME}' exists: {e}")
        return False
    finally:
        if 'connection_to_server' in locals() and connection_to_server:
            connection_to_server.close()


def initialise_database_schema():
  
    if not ensure_database_exists():
        print("DB Init: Critical error: Database could not be ensured. Aborting schema initialization.")
        return False 

    connection = get_db_connection() # Now connect to the specific database
    if not connection:
        print("DB Init: Failed to connect to the database after ensuring it exists. Cannot create table.")
        return False

    try:
        with connection.cursor() as cursor:
            print(f"DB Init: Attempting to create table 'sensor_data' if it does not exist in '{DB_NAME}'...")
            cursor.execute(CREATE_SENSOR_DATA_TABLE_SQL)
            print("DB Init: 'sensor_data' table creation command executed.")

            try:
                cursor.execute("SHOW INDEX FROM `sensor_data` WHERE Key_name = 'idx_created_at'")
                if not cursor.fetchone():
                    print(f"DB Init: Index 'idx_created_at' not found, creating it...")
                    cursor.execute("CREATE INDEX `idx_created_at` ON `sensor_data` (`created_at`);")
                    print("DB Init: Index 'idx_created_at' creation command executed.")
                else:
                    print("DB Init: Index 'idx_created_at' already exists.")
            except pymysql.MySQLError as index_e:
                print(f"DB Init: Notice during index creation for 'idx_created_at': {index_e} (May be benign if index already exists or table was just created)")

        connection.commit()
        print("DB Init: Database schema initialization complete.")
        return True
    except pymysql.MySQLError as e:
        print(f"DB Init: Error during schema initialization: {e}")
        return False
    finally:
        if connection:
            connection.close()

def insert_sensor_data(temperature: float, humidity: float, light_level: int):
    if None in [temperature, humidity, light_level]:
        return

    connection = get_db_connection()
    if not connection:
        print("DB Insert: Failed to get database connection. Data not inserted.")
        return

    try:
        with connection.cursor() as cursor:
            sql = "INSERT INTO `sensor_data` (`temperature`, `humidity`, `light_level`) VALUES (%s, %s, %s)"
            cursor.execute(sql, (temperature, humidity, light_level))
        connection.commit()
    except pymysql.MySQLError as e:
        print(f"DB Insert: Error inserting data: {e}")
    finally:
        if connection:
            connection.close()

def get_historical_sensor_data(limit: int = 100):
    connection = get_db_connection()
    if not connection:
        print("DB Fetch: Failed to get database connection.")
        return []
    try:
        with connection.cursor() as cursor:
            sql = "SELECT `temperature`, `humidity`, `light_level`, `created_at` FROM `sensor_data` ORDER BY `created_at` DESC LIMIT %s"
            cursor.execute(sql, (limit,))
            results = cursor.fetchall()
            for row in results:
                if isinstance(row.get('created_at'), datetime.datetime):
                    row['created_at'] = row['created_at'].isoformat()
            return results
    except pymysql.MySQLError as e:
        print(f"DB Fetch: Error fetching historical data: {e}")
        return []
    finally:
        if connection:
            connection.close()

if __name__ == '__main__':
    print("Testing database_utils.py with schema initialization...")
    print(f"DB Config: Host={DB_HOST}, User={DB_USER}, DB={DB_NAME}, Port={DB_PORT}")

    if initialise_database_schema(): 
        print("\nSchema initialization attempt finished.")
        try:
            print("Attempting to insert test data...")
            insert_sensor_data(10.1, 20.2, 303)
            print("Test data insertion attempted.")

            print("\nAttempting to fetch last 5 records...")
            historical_data = get_historical_sensor_data(limit=5)
            if historical_data:
                print("Fetched data:")
                for record in historical_data:
                    print(record)
            else:
                print("No historical data found or error fetching.")
        except Exception as e:
            print(f"Error during data operations: {e}")
    else:
        print("Could not initialize database schema. Aborting further tests.")

    print("\nDatabase utils test finished.")