// main.js – Electron entry point with robust backend readiness handling
// This file has been updated to replace fragile stdout string detection with a health‑endpoint poll.
// It also shows a user‑friendly error dialog if the backend cannot start (e.g., DB unreachable).

const { app, BrowserWindow, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');

let backendProcess = null;
let mainWindow = null;

// Configuration – you can adjust these values if you change the backend port.
const BACKEND_HOST = '127.0.0.1';
const BACKEND_PORT = 8000; // keep in sync with the spawn args below
const BACKEND_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}`;
const HEALTH_ENDPOINT = `${BACKEND_URL}/health`;
const BACKEND_START_TIMEOUT_MS = 30000; // 30 seconds

/**
 * Starts the Python FastAPI backend inside the packaged virtual‑environment.
 * After spawning the process we repeatedly poll the health endpoint until it
 * returns HTTP 200, then we create the Electron window.
 */
function startBackend() {
  const backendDir = app.isPackaged
    ? path.join(process.resourcesPath, 'python-backend')
    : path.join(__dirname, 'python-backend');
  const venvPython = path.join(backendDir, 'venv', 'Scripts', 'python.exe');
  const args = [
    '-m',
    'uvicorn',
    'api.main:app',
    '--host',
    BACKEND_HOST,
    '--port',
    String(BACKEND_PORT),
  ];
  const env = { ...process.env, PYTHONUNBUFFERED: '1' };

  backendProcess = spawn(venvPython, args, {
    cwd: backendDir,
    env,
    windowsHide: true,
  });

  backendProcess.stdout.on('data', (data) => console.log(`[backend STDOUT] ${data}`));
  backendProcess.stderr.on('data', (data) => console.error(`[backend STDERR] ${data}`));

  backendProcess.on('exit', (code, signal) => {
    console.log(`Backend exited with ${code || signal}`);
    // If the window is still open, close it – the app will quit soon.
    if (mainWindow) mainWindow.close();
  });

  // Begin polling the health endpoint – this is far more reliable than
  // trying to parse uvicorn’s log output.
  waitForBackendReady();
}

/**
 * Polls the FastAPI health endpoint until it returns 200 OK or the timeout
 * expires. On success we launch the UI; on failure we show an error dialog.
 */
function waitForBackendReady() {
  const startTime = Date.now();
  const poll = async () => {
    try {
      // Node >= 18 has a built‑in fetch; otherwise a polyfill would be needed.
      const response = await fetch(HEALTH_ENDPOINT);
      if (response.ok) {
        createWindow();
        return; // ready – stop polling
      }
    } catch (e) {
      // Network errors are expected while the server is still starting.
    }
    if (Date.now() - startTime > BACKEND_START_TIMEOUT_MS) {
      showStartupError(
        'Unable to start the Prabha Dairy backend within 30 seconds.\n\n' +
          'Common reasons:\n' +
          '- PostgreSQL is not running or the connection string in .env is incorrect.\n' +
          '- Port 8000 is already in use.\n' +
          '- Required Python dependencies failed to install.'
      );
      return;
    }
    // Retry after a short pause.
    setTimeout(poll, 500);
  };
  poll();
}

/**
 * Shows a modal error box and quits the app.
 */
function showStartupError(message) {
  dialog.showErrorBox('Prabha Dairy – Startup Error', message);
  app.quit();
}

/**
 * Creates the main Electron window once the backend reports healthy.
 */
function createWindow() {
  if (mainWindow) return; // already created
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  // The FastAPI server serves the UI on the same port.
  mainWindow.loadURL(`${BACKEND_URL}`);

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

/**
 * Gracefully stops the backend process.
 */
function stopBackend() {
  if (backendProcess) {
    backendProcess.kill('SIGTERM');
    backendProcess = null;
  }
}

app.whenReady().then(startBackend);

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    stopBackend();
    app.quit();
  }
});

app.on('before-quit', stopBackend);

app.on('activate', () => {
  if (!mainWindow) createWindow();
});
