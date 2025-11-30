from setuptools import setup, find_packages

setup(
    name="aria-cli",
    version="0.2.0",
    packages=find_packages(),
    install_requires=[
        "httpx>=0.27.0",
        "click>=8.1.0",
        "rich>=13.7.0",
    ],
    entry_points={
        "console_scripts": [
            "aria=aria_cli.main:cli",
        ],
    },
    python_requires=">=3.10",
)
