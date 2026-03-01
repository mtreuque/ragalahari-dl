from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="ragalahari-dl",
    version="2.0.0",
    author="corinovate",
    author_email="",
    description="A powerful CLI tool to browse and bulk download HD photo galleries from Ragalahari.com",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/corinovate/ragalahari-dl",
    py_modules=["ragalahari_dl"],
    python_requires=">=3.7",
    install_requires=[
        "requests>=2.28.0",
        "beautifulsoup4>=4.11.0",
    ],
    entry_points={
        "console_scripts": [
            "ragalahari-dl=ragalahari_dl:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Environment :: Console",
        "Topic :: Internet :: WWW/HTTP",
        "Topic :: Multimedia :: Graphics",
    ],
    keywords="ragalahari downloader gallery images photos bulk batch",
)
