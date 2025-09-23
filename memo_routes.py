from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, current_app
import sqlite3
import json
from datetime import datetime
import os
import logging

memo_bp = Blueprint('memo', __name__, url_prefix='/memo')

def get_db_path():
    """Get the path to the memo database file"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'memo.db')

def init_memo_db():
    """Initialize the memo database with required tables and columns"""
    db_path = get_db_path()
    logger = current_app.logger if current_app else None
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Create table if it doesn't exist
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS memos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                column_name TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Check if created_by column exists
        cursor.execute("PRAGMA table_info(memos)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'created_by' not in columns:
            if logger:
                logger.info("Adding created_by column to memos table...")
            else:
                print("Adding created_by column...")
            cursor.execute('ALTER TABLE memos ADD COLUMN created_by TEXT')
            if logger:
                logger.info("Column added successfully")
            else:
                print("Column added successfully")
        else:
            print("created_by column already exists")
            
        conn.commit()
        conn.close()
        
        if logger:
            logger.info("Database initialization completed successfully")
        
        return True
        
    except Exception as e:
        error_msg = f"Error initializing database: {e}"
        if logger:
            logger.error(error_msg)
        else:
            print(error_msg)
        return False
@memo_bp.route('/')
def memo_page():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))
    
    current_user = session['username']
    
    # Initialize database when accessing memo page
    
    return render_template('memo.html', current_user=current_user)

@memo_bp.route('/add', methods=['POST'])
def add_memo():
    if 'username' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    logger = current_app.logger
    
    try:
        data = request.get_json()
        current_user = session.get('username', 'Unknown')
        
        logger.info(f"Adding memo by user {current_user}: {data.get('title', 'No title')}")
        
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO memos (title, description, column_name, timestamp, created_by)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            data['title'],
            data.get('description', ''),
            data['column'],
            data['timestamp'],
            current_user
        ))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Memo added successfully by {current_user}")
        return jsonify({'success': True})
    
    except Exception as e:
        logger.error(f"Error in add_memo: {e}")
        return jsonify({'success': False, 'error': str(e)})

@memo_bp.route('/move', methods=['POST'])
def move_memo():
    if 'username' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    logger = current_app.logger
    
    try:
        data = request.get_json()
        card_id = data['card_id']
        new_column = data['new_column']
        current_user = session.get('username', 'Unknown')
        
        logger.info(f"Moving memo {card_id} to column {new_column} by user: {current_user}")
        
        valid_columns = ['todo', 'progress', 'review', 'done']
        if new_column not in valid_columns:
            logger.warning(f"Invalid column name attempted: {new_column}")
            return jsonify({'success': False, 'error': 'Invalid column name'})
        
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE memos 
            SET column_name = ?, created_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (new_column, card_id))
        
        if cursor.rowcount == 0:
            conn.close()
            logger.warning(f"Attempted to move non-existent memo: {card_id}")
            return jsonify({'success': False, 'error': 'Card not found'})
        
        conn.commit()
        conn.close()
        
        logger.info(f"Memo {card_id} moved successfully to {new_column}")
        return jsonify({'success': True})
    
    except Exception as e:
        logger.error(f"Error in move_memo: {e}")
        return jsonify({'success': False, 'error': str(e)})

@memo_bp.route('/list')
def list_memos():
    if 'username' not in session:
        return jsonify({'memos': [], 'error': 'Unauthorized'}), 401
    
    logger = current_app.logger
    current_user = session.get('username', 'Unknown')
    
    try:        
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if created_by column exists before selecting it
        cursor.execute("PRAGMA table_info(memos)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'created_by' in columns:
            cursor.execute('''
                SELECT id, title, description, column_name, timestamp, created_by
                FROM memos
                ORDER BY created_at DESC
            ''')
        else:
            cursor.execute('''
                SELECT id, title, description, column_name, timestamp
                FROM memos
                ORDER BY created_at DESC
            ''')
        
        memos = []
        for row in cursor.fetchall():
            memo = {
                'id': row[0],
                'title': row[1],
                'description': row[2],
                'column': row[3],
                'timestamp': row[4],
                'created_by': row[5] if len(row) > 5 and row[5] else 'Unknown'
            }
            memos.append(memo)
        
        conn.close()
        return jsonify({'memos': memos})
    
    except Exception as e:
        logger.error(f"Error in list_memos: {e}")
        return jsonify({'memos': [], 'error': str(e)})

@memo_bp.route('/delete/<int:memo_id>', methods=['DELETE'])
def delete_memo(memo_id):
    if 'username' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    logger = current_app.logger
    current_user = session.get('username', 'Unknown')
    
    try:
        logger.info(f"Deleting memo {memo_id} by user: {current_user}")
        
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM memos WHERE id = ?', (memo_id,))
        
        if cursor.rowcount == 0:
            conn.close()
            logger.warning(f"Attempted to delete non-existent memo: {memo_id}")
            return jsonify({'success': False, 'error': 'Card not found'})
        
        conn.commit()
        conn.close()
        
        logger.info(f"Memo {memo_id} deleted successfully")
        return jsonify({'success': True})
    
    except Exception as e:
        logger.error(f"Error in delete_memo: {e}")
        return jsonify({'success': False, 'error': str(e)})
