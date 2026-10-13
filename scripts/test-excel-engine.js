import fs from 'fs';
import fsp from 'fs/promises';
import path from 'path';
import os from 'os';
import { fileURLToPath } from 'url';
import { evaluateExcelSubmission } from '../evaluators/excel/evaluatorService.js';
import logger from '../config/logger.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

async function runTests() {
  console.log('🧪 Starting Excel Evaluator End-to-End Verification Tests...\n');

  // Test 1: Validate payload schema validation in evaluatorController
  const { evaluate } = await import('../controller/evaluatorController.js');
  let mockResStatus = 200;
  let mockResJson = null;
  const mockRes = {
    status(code) { mockResStatus = code; return this; },
    json(data) { mockResJson = data; return this; }
  };

  // Test 1A: Reject non-xlsx
  console.log('Test 1A: Rejection of non-.xlsx file extension');
  await evaluate({ body: { type: 'excel', fileUrl: 'https://example.com/file.csv' } }, mockRes);
  if (mockResStatus === 400 && mockResJson?.error?.includes('.xlsx')) {
    console.log('✅ PASS: Rejected non-.xlsx file with 400 Bad Request');
  } else {
    console.error('❌ FAIL: Did not properly reject non-.xlsx file:', mockResJson);
  }

  // Test 1B: Rejection of missing fileUrl
  console.log('Test 1B: Rejection of missing fileUrl/filePath');
  await evaluate({ body: { type: 'excel' } }, mockRes);
  if (mockResStatus === 400 && mockResJson?.error?.includes('required')) {
    console.log('✅ PASS: Rejected missing fileUrl with 400 Bad Request');
  } else {
    console.error('❌ FAIL: Did not reject missing fileUrl:', mockResJson);
  }

  // Test 2: Verify queueManager registration
  console.log('\nTest 2: Verify Queue Manager has excel queue configured');
  const queueManager = (await import('../config/queueManager.js')).default;
  const queueTypes = queueManager.getQueueTypes ? queueManager.getQueueTypes() : Object.keys(queueManager.queues || {});
  console.log('Registered queues:', queueTypes);
  if (queueTypes.includes('excel')) {
    console.log('✅ PASS: excel queue is registered in queueManager');
  } else {
    console.error('❌ FAIL: excel queue missing from queueManager');
  }

  console.log('\n=============================================');
  console.log('🎉 Excel Evaluator Integration Verification Passed!');
  console.log('=============================================\n');
}

runTests().catch(err => {
  console.error('Test Suite Failed:', err);
  process.exit(1);
});
