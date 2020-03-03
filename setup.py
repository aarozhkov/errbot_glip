import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="errbot_glip",  # Replace with your own username
    version="0.0.1",
    author="Aleksandr Rozhkov",
    author_email="alexander.rozhkov@nordigy.ru",
    description="Glip integration for Errbot framework",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://git.ringcentral.com/rcvdevops/errbot_glip.git",
    packages=setuptools.find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GPL",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.6',
)
