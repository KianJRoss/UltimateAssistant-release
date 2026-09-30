"""Small setuptools hook for source-only private developer-loop modules."""
from setuptools import setup
from setuptools.command.build_py import build_py


class PublicBuildPy(build_py):
    _PRIVATE_MODULES = {"herald.router.admin_loop"}

    def find_package_modules(self, package, package_dir):
        modules = super().find_package_modules(package, package_dir)
        return [
            entry for entry in modules
            if f"{package}.{entry[1]}" not in self._PRIVATE_MODULES
        ]


setup(cmdclass={"build_py": PublicBuildPy})
