module.exports = {
  apps: [
    {
      name: 'pi-agent',
      cwd: '/home/user/webapp/app',
      script: 'python3',
      args: '-m uvicorn main:app --host 0.0.0.0 --port 3000',
      env: { PYTHONUNBUFFERED: '1' },
      watch: false,
      instances: 1,
      exec_mode: 'fork',
    },
  ],
};
