"""Auto-generated setup.py for py2app. Do not edit manually."""
import sys
sys.path.insert(0, '/Users/bigchafo/Documents/spx-income-trader')

from setuptools import setup

APP = ['/Users/bigchafo/Documents/spx-income-trader/app_desktop.py']
DATA_FILES = [
    ('dashboard/templates', ['/Users/bigchafo/Documents/spx-income-trader/dashboard/templates/index.html', '/Users/bigchafo/Documents/spx-income-trader/dashboard/templates/settings.html', '/Users/bigchafo/Documents/spx-income-trader/dashboard/templates/setup.html']),
    ('config', ['/Users/bigchafo/Documents/spx-income-trader/config/strategy_params.yaml']),
    ('assets', ['/Users/bigchafo/Documents/spx-income-trader/assets/icon.png']),
    ('dashboard/static', ['/Users/bigchafo/Documents/spx-income-trader/dashboard/static/icon.png']),
]
OPTIONS = {'argv_emulation': False, 'iconfile': '/Users/bigchafo/Documents/spx-income-trader/assets/icon.icns', 'plist': {'CFBundleName': 'The Daily Melt', 'CFBundleDisplayName': 'The Daily Melt', 'CFBundleIdentifier': 'com.spxincometrader.app', 'CFBundleVersion': '1.0.0', 'CFBundleShortVersionString': '1.0.0', 'CFBundleExecutable': 'TheDailyMelt', 'CFBundlePackageType': 'APPL', 'CFBundleSignature': '????', 'LSUIElement': False, 'NSHighResolutionCapable': True, 'LSMinimumSystemVersion': '10.15', 'LSApplicationCategoryType': 'public.app-category.finance', 'NSAppleEventsUsageDescription': 'SPX Income Trader needs automation access.'}, 'packages': ['src', 'src.brokers', 'src.core', 'src.data', 'src.models', 'src.utils', 'dashboard', 'config', 'database', 'flask', 'jinja2', 'sqlalchemy', 'keyring', 'plotly', 'yaml', 'engineio', 'requests', 'requests_oauthlib', 'platformdirs', 'dotenv', 'pytz', 'yfinance', 'pystray', 'PIL', 'schwab', 'authlib', 'httpx', 'charset_normalizer'], 'includes': ['keyring.backends', 'keyring.backends.macOS', 'webview', 'engineio.async_drivers.threading', 'jinja2.ext', 'sqlalchemy.dialects.sqlite', 'charset_normalizer.md__mypyc'], 'excludes': ['pytest', 'black', 'flake8', 'pytest_cov', 'tkinter', 'matplotlib'], 'resources': [], 'site_packages': True, 'strip': True, 'optimize': 2}

setup(
    name='The Daily Melt',
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
