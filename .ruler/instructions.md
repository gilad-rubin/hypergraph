**This project is in design mode. Only markdown is needed**
**Updated design specs are in specs/reviewed. Please search and read there when not sure**

see @specs/reviewed/README.md for the overview

## General
- use uv run X to run scripts
- use trash X instead of rm X. This allows me to rescue deleted files if I need.
- **commit frequently** using "Conventional Commits" https://www.conventionalcommits.org/en/v1.0.0/
- when we're discussing design and building docs, use ELI5 (but more like ELI20) to explain the issue and considerations. one good example is usually worth a 1000 words. **use this frequently** when something is not working or when we're considering alternatives.
- when making changes in the codebase - run them to verify everything works
- if an API key is needed, first check in the .env to make sure it exists. use dotenv to load if needed.
- prefer to search online and in documentations before acting
- tests go to tests folder. scripts go to scripts folder
- when you're finished with a task - you can write a summary, but just one is enough. no need for multiple summaries and markdowns.
- update .ruler/2-code-structure.md if you've changed something in the code structure :)

## Coding Principles
- When designing and implementing features - always prefer using SOLID principles.
- Use simple, human readable functions rather than massive long indented functions.
- Split classes functions into helper functions if needed

## Tools
- use context7 and MCP servers whenever you're stuck or want to understand how a library works

## Jupyter
- Use concise, human readable cells
- avoid redundancy in notebooks. keep the cells and notebook as a whole concise.
- avoid using "special" emojis in jupyter, it can crash the notebook. you can use basic ones, like X, V etc...
- remember that jupyter has its own async handling. remember to use the correct syntax.
- If you're editing a module while reviewing the output in jupyter, remember to either restart the kernel or reload the module to see changes

- jupyter notebook's working directory is the project's working directory, so no need to do sys.path.insert(0, '/Users/...')
- run cells after you create them to verify things work as expected. read the output and decide what to do next
- when trying to figure out something about an object - iterate over it by running the cell and examining the output and refining