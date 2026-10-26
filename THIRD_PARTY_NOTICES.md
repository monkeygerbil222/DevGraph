# Third-party notices

DevGraph ships the following third-party code in its package.

## Cytoscape.js 3.30.2

- File: `devgraph/dashboard/static/vendor/cytoscape-3.30.2.min.js`, unmodified `dist/cytoscape.min.js` from the npm package `cytoscape@3.30.2`
  - File hash: sha256 `83e8c54a6bec655bfd81df07df605649c268af69aeca67a5ea2da54ea42dac81`
  - npm tarball integrity (hash of the `cytoscape-3.30.2.tgz` it was extracted from, not of the file): `sha512-oICxQsjW8uSaRmn4UK/jkczKOqTrVqt5/1WL0POiJUT2EKNc9STM4hYFHv917yu55aTBMFNRzymlJhVAiWPCxw==`
- Source: https://github.com/cytoscape/cytoscape.js
- License: MIT

```
Copyright (c) 2016-2024, The Cytoscape Consortium.

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the “Software”), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## sigma.js 4.0.3

- File: `devgraph/dashboard/static/vendor/sigma-4.0.3.min.js`, unmodified `dist/sigma.min.js` (its UMD build, global `Sigma`) from the npm package `sigma@4.0.3`
  - File hash: sha256 `694694451bff0ebb4301768e14344c4601de2e52e180a655e4ceaf1644cf43b2`
  - npm tarball integrity (hash of the `sigma-4.0.3.tgz` it was extracted from, not of the file): `sha512-DwtnWVZMpHzgfyWwyH9bvNAV2DwSl9UZHmieLIAOdZboQAfjEGwcKNJzLQOca/vrKNiUYexVnPcNzhkNUQHdVA==`
  - The build bundles two of sigma's dependencies, under the notices below: `events` 3.3.0 and `graphology-utils` 2.5.2.
- Source: https://github.com/jacomyal/sigma.js
- License: MIT

```
Copyright (C) 2012-2026, Alexis Jacomy, Guillaume Plique, Benoît Simard https://www.sigmajs.org

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
IN THE SOFTWARE.
```

### events 3.3.0 (bundled in sigma-4.0.3.min.js and graphology-0.26.0.umd.min.js)

- Source: https://github.com/browserify/events
- License: MIT

```
MIT

Copyright Joyent, Inc. and other Node contributors.

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to permit
persons to whom the Software is furnished to do so, subject to the
following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
USE OR OTHER DEALINGS IN THE SOFTWARE.
```

### graphology-utils 2.5.2 (bundled in sigma-4.0.3.min.js)

- Source: https://github.com/graphology/graphology
- License: MIT

```
The MIT License (MIT)

Copyright (c) 2017-2021 Guillaume Plique (Yomguithereal)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

## graphology 0.26.0

- File: `devgraph/dashboard/static/vendor/graphology-0.26.0.umd.min.js`, unmodified `dist/graphology.umd.min.js` (global `graphology`) from the npm package `graphology@0.26.0`
  - File hash: sha256 `dc337efa23903f61e064c8e7e7f93a429e6855dccfc2458802b4ed30c621c087`
  - npm tarball integrity (hash of the `graphology-0.26.0.tgz` it was extracted from, not of the file): `sha512-8SSImzgUUYC89Z042s+0r/vMibY7GX/Emz4LDO5e7jYXhuoWfHISPFJYjpRLUSJGq6UQ6xlenvX1p/hJdfXuXg==`
- Source: https://github.com/graphology/graphology
- License: MIT

```
The MIT License (MIT)

Copyright (c) 2016-2021 Guillaume Plique (Yomguithereal)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
