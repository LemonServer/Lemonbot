import sqlite3
import os
class Storage:
    def __init__(self, db_path):
        db_dir = os.path.dirname(db_path)
        if not os.path.exists(db_dir):
            os.makedirs(db_dir)
        self.conn = sqlite3.connect(db_path)  # 连接到数据库文件
        self.create_table()  # 创建表


    def create_table(self):
        query = '''
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_message TEXT,
            bot_response TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        '''
        self.conn.execute(query)  # 执行SQL语句
        self.conn.commit()  # 提交事务

    def store_conversation(self, user_message, bot_response):
        query = 'INSERT INTO conversations (user_message, bot_response) VALUES (?, ?)'
        self.conn.execute(query, (user_message, bot_response))  # 执行插入操作
        self.conn.commit()  # 提交事务