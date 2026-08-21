
from pathlib import Path
import shutil


class PhysicalDeleteAdapter:


    def delete_path(
        self,
        path,
    ):

        if path is None:
            return {
                "deleted": False,
                "reason": "INVALID_PATH",
            }


        target = Path(
            path
        )


        if not target.exists():
            return {
                "deleted": False,
                "status": "NOT_FOUND",
                "message":
                    "target does not exist",
                "path":
                    str(target),
            }


        try:

            if target.is_dir():

                shutil.rmtree(
                    target
                )

            else:

                target.unlink()


        except Exception as exc:

            return {
                "deleted": False,
                "status": "FAILED",
                "message":
                    str(exc),
                "path":
                    str(target),
            }


        return {
            "deleted": True,
            "status": "SUCCESS",
            "path": str(target),
        }
