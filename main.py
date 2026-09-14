import asyncio
import os

os.environ["PROJECT_ROOT"] = os.path.dirname(os.path.abspath(__file__))

from conic.entry import main

if __name__ == "__main__":
    asyncio.run(main())
