"""Manager-first Streamlit entry point.



Forces manager workspace mode (Schedule | Analytics | Print) and hides ops/dev

surfaces such as Sentry, stress-test, and jurisdiction swap.

"""



from __future__ import annotations



import os



# Process-level flag — survives Streamlit reruns before set_page_config runs.

os.environ["LAB_SCHEDULER_MANAGER_ENTRY"] = "1"



from app import main



if __name__ == "__main__":

    main()

