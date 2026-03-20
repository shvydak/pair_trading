const path = require('path');

module.exports = {
  apps: [
    {
      name: 'pair-trading',
      script: path.join(__dirname, '.venv/bin/uvicorn'),
      args: 'main:app --host 0.0.0.0 --port 8080',
      cwd: path.join(__dirname, 'backend'),
      interpreter: 'none',
      watch: false,
      env: {
        PYTHONUNBUFFERED: '1',
      },
    },
  ],
};
