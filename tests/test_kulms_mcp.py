import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from kulms_submit.KulmsMCP import create_server
from kulms_submit.KulmsSources import snapshot_note, render_note, note_digest
from unittest.mock import patch


class MCPTests(unittest.TestCase):
    def test_schema_tools_and_annotations(self):
        with tempfile.TemporaryDirectory() as root:
            tools = asyncio.run(create_server(root).list_tools())
            by_name = {t.name: t for t in tools}
            self.assertEqual(len(tools), 11)
            self.assertFalse(by_name['submit_reviewed_assignment'].annotations.readOnlyHint)
            self.assertTrue(by_name['submit_reviewed_assignment'].annotations.destructiveHint)
            schema = by_name['record_submission_review'].inputSchema
            self.assertEqual(set(schema['$defs']['Checks']['required']), {'filename', 'identity', 'coverage', 'readability'})
            self.assertIn('completion_signal', by_name['submit_reviewed_assignment'].inputSchema['required'])

    def test_real_stdio_initialize_discovery_and_local_tool(self):
        with tempfile.TemporaryDirectory() as root:
            async def exercise():
                params = StdioServerParameters(command=sys.executable, args=['-m', 'kulms_submit.KulmsMCP', '--state-dir', root])
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        initialized = await session.initialize()
                        self.assertEqual(initialized.serverInfo.name, 'KULMS Submission')
                        self.assertIn('human user', initialized.instructions)
                        listed = await session.list_tools()
                        self.assertEqual(len(listed.tools), 11)
                        result = await session.call_tool('get_student_profile', {})
                        self.assertFalse(result.isError)
                        updated = await session.call_tool('set_student_profile', {
                            'name': 'Test Student', 'student_id': '123', 'affiliation': 'B3'})
                        self.assertFalse(updated.isError)
                        blocked = await session.call_tool('submit_reviewed_assignment', {
                            'job_id': '0' * 32, 'completion_signal': 'Submit'})
                        self.assertTrue(blocked.isError)
            asyncio.run(asyncio.wait_for(exercise(), timeout=20))

    def test_running_editor_failure_does_not_fall_back_to_old_disk(self):
        with patch('kulms_submit.KulmsSources.default_connection_file') as connection:
            connection.return_value.exists.return_value = True
            with patch('kulms_submit.KulmsSources.NotebookClient') as client:
                client.return_value.call.side_effect = ValueError('old editor')
                with self.assertRaisesRegex(ValueError, '最新保存'):
                    snapshot_note('note-1')
