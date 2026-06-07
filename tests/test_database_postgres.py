import os
import json
import pytest
from unittest.mock import MagicMock, patch
import src.config
from src.database import translate_sqlite_to_postgres, JanusConnectionWrapper, JanusCursorWrapper, get_connection
from src.memory import PgVectorCollectionWrapper, get_collection

def test_sql_translation_autoincrement():
    sqlite_sql = "CREATE TABLE test (id INTEGER PRIMARY KEY AUTOINCREMENT);"
    pg_sql = translate_sqlite_to_postgres(sqlite_sql)
    assert "SERIAL PRIMARY KEY" in pg_sql
    assert "AUTOINCREMENT" not in pg_sql

def test_sql_translation_insert_ignore():
    sqlite_sql = "INSERT OR IGNORE INTO system_config (config_key, config_value) VALUES (?, ?);"
    pg_sql = translate_sqlite_to_postgres(sqlite_sql)
    assert "INSERT INTO system_config" in pg_sql
    assert "ON CONFLICT (config_key) DO NOTHING" in pg_sql
    assert "%s" in pg_sql
    assert "?" not in pg_sql

def test_sql_translation_insert_replace():
    sqlite_sql = "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) VALUES (?, ?, ?);"
    pg_sql = translate_sqlite_to_postgres(sqlite_sql)
    assert "INSERT INTO system_config" in pg_sql
    assert "ON CONFLICT (config_key) DO UPDATE SET config_value = EXCLUDED.config_value, is_agent_modifiable = EXCLUDED.is_agent_modifiable" in pg_sql
    assert "%s" in pg_sql
    assert "?" not in pg_sql

def test_sql_translation_placeholders_outside_strings():
    # Make sure ? inside single quotes are NOT replaced
    sql = "SELECT * FROM test WHERE val = ? AND name = 'what is this ?';"
    translated = translate_sqlite_to_postgres(sql)
    assert translated == "SELECT * FROM test WHERE val = %s AND name = 'what is this ?';"

def test_sql_translation_pragma_ignored():
    sql = "PRAGMA journal_mode=WAL;"
    translated = translate_sqlite_to_postgres(sql)
    assert translated == "SELECT 1"

def test_write_protection_core_constitution():
    # Cursor wrapper should raise PermissionError when read_only_constitution=True and write is attempted
    mock_cursor = MagicMock()
    wrapper = JanusCursorWrapper(mock_cursor, db_type="postgres", read_only_constitution=True)
    
    with pytest.raises(PermissionError):
        wrapper.execute("INSERT INTO core_constitution (rule_key, rule_text) VALUES (%s, %s);")
        
    with pytest.raises(PermissionError):
        wrapper.execute("UPDATE core_constitution SET rule_text = %s;")

def test_no_write_protection_when_admin():
    mock_cursor = MagicMock()
    wrapper = JanusCursorWrapper(mock_cursor, db_type="postgres", read_only_constitution=False)
    
    # Should not raise exception
    wrapper.execute("INSERT INTO core_constitution (rule_key, rule_text) VALUES (%s, %s);")
    assert mock_cursor.execute.called

def test_pg_vector_collection_add():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    
    with patch("src.database.get_connection", return_value=mock_conn):
        wrapper = PgVectorCollectionWrapper("test_collection")
        wrapper.add(
            documents=["doc1"],
            metadatas=[{"key": "val"}],
            ids=["id1"],
            embeddings=[[0.1, 0.2]]
        )
        
        # Verify psycopg2 connection calls
        mock_cur.execute.assert_called_once()
        args = mock_cur.execute.call_args[0]
        assert "INSERT INTO janus_embeddings" in args[0]
        assert args[1][0] == "test_collection"
        assert args[1][1] == "id1"
        assert args[1][2] == "doc1"
        assert "key" in args[1][3]
        assert "[0.1,0.2]" in args[1][4]

def test_pg_vector_collection_query():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = [("id1", "doc1", '{"key": "val"}', 0.1)]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    
    with patch("src.database.get_connection", return_value=mock_conn):
        wrapper = PgVectorCollectionWrapper("test_collection")
        res = wrapper.query(
            query_embeddings=[[0.1, 0.2]],
            n_results=5,
            where={"key": "val"}
        )
        
        # Check output matching ChromaDB format
        assert res["ids"] == [["id1"]]
        assert res["documents"] == [["doc1"]]
        assert res["metadatas"] == [[{"key": "val"}]]
        assert res["distances"] == [[0.1]]
        
        mock_cur.execute.assert_called_once()
        sql_arg = mock_cur.execute.call_args[0][0]
        params_arg = mock_cur.execute.call_args[0][1]
        assert "ORDER BY distance ASC" in sql_arg
        assert "metadata ->> %s = %s" in sql_arg
        assert params_arg[0] == "[0.1,0.2]"
        assert params_arg[1] == "test_collection"

def test_pg_vector_collection_get():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = [("id1", "doc1", '{"key": "val"}')]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    
    with patch("src.database.get_connection", return_value=mock_conn):
        wrapper = PgVectorCollectionWrapper("test_collection")
        res = wrapper.get(where={"key": "val"})
        
        assert res["ids"] == ["id1"]
        assert res["documents"] == ["doc1"]
        assert res["metadatas"] == [{"key": "val"}]

def test_pg_vector_collection_update():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    # Mock select metadata first
    mock_cur.fetchone.return_value = ('{"old_key": "old_val"}',)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    
    with patch("src.database.get_connection", return_value=mock_conn):
        wrapper = PgVectorCollectionWrapper("test_collection")
        wrapper.update(ids=["id1"], metadatas=[{"new_key": "new_val"}])
        
        # Verify update SQL execution
        # First call is SELECT, second call is UPDATE
        assert mock_cur.execute.call_count == 2
        select_sql = mock_cur.execute.call_args_list[0][0][0]
        update_sql = mock_cur.execute.call_args_list[1][0][0]
        update_params = mock_cur.execute.call_args_list[1][0][1]
        
        assert "SELECT metadata" in select_sql
        assert "UPDATE janus_embeddings SET metadata" in update_sql
        # Verify keys merged in parameters
        metadata_dict = json.loads(update_params[0])
        assert metadata_dict["old_key"] == "old_val"
        assert metadata_dict["new_key"] == "new_val"

def test_pg_vector_collection_upsert():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    
    with patch("src.database.get_connection", return_value=mock_conn):
        wrapper = PgVectorCollectionWrapper("test_collection")
        wrapper.upsert(
            documents=["doc1"],
            metadatas=[{"key": "val"}],
            ids=["id1"],
            embeddings=[[0.1, 0.2]]
        )
        
        mock_cur.execute.assert_called_once()
        sql = mock_cur.execute.call_args[0][0]
        assert "ON CONFLICT (collection_name, id) DO UPDATE SET" in sql
