"""UI support that does not depend on Streamlit.

Pages are thin: they collect input and call into these modules and the services. Keeping the
logic here means it is tested without a browser, and the pages contain no SQL, parsing or maths.
"""
