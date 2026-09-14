from pathlib import Path
import sys
source=Path(sys.argv[1]);target=Path(sys.argv[2])
assert source.is_symlink() and source.resolve()==target.resolve(),(str(source),str(source.resolve()),str(target.resolve()))
